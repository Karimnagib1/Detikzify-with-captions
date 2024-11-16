#!/usr/bin/env -S torchrun --nproc_per_node gpu
from argparse import ArgumentParser
from collections import defaultdict
from datetime import timedelta
from functools import partial
from itertools import count
from json import dump, load as load_json
from operator import itemgetter
from os import getenv
from os.path import isfile, join
from time import time

from datasets import load_dataset
from numpy import array
from scipy.stats.mstats import winsorize
from torch import bfloat16, distributed as dist, float16
from torch.cuda import is_available as is_cuda_available, is_bf16_supported
from tqdm import tqdm
from transformers import set_seed
from transformers.utils import is_flash_attn_2_available

from detikzify.evaluate import (
    CrystalBLEU,
    KernelInceptionDistance,
    ImageSim,
    TexEditDistance,
    DreamSim,
)
from detikzify.infer import DetikzifyPipeline, TikzDocument
from detikzify.model import load as load_model
from detikzify.model import load_lora

WORLD_SIZE = int(getenv("WORLD_SIZE", 1))
RANK = int(getenv("RANK", 0))

def parse_args():
    argument_parser = ArgumentParser(
        description="Evaluate fine-tuned models."
    )
    argument_parser.add_argument(
        "--cache_dir",
        help="directory where model outputs should be saved to",
    )
    argument_parser.add_argument(
        "--trainset",
        required=True,
        help="path to the datikz train set (in parquet format)",
    )
    argument_parser.add_argument(
        "--testset",
        required=True,
        help="path to the datikz test set (in parquet format)",
    )
    argument_parser.add_argument(
        "--output",
        required=True,
        help="where to save the computed scores (as json)",
    )
    argument_parser.add_argument(
        "--timeout",
        type=int,
        help="minimum time to run MCTS in seconds",
    )
    # argument_parser.add_argument(
    #     "--lora_weights",
    #     help="path to lora weights directory",
    #     required=True
    # )
    argument_parser.add_argument(
        "--use_sketches",
        action="store_true",
        help="condition model on sketches instead of images",
    )
    # argument_parser.add_argument(
    #     "--path",
    #     nargs='+',
    #     metavar="MODEL=PATH",
    #     required=True,
    #     help="(multiple) key-value pairs of model names and paths/urls to models/adapters (local or hub) or json files",
    # )
    argument_parser.add_argument(
        "--path",
        nargs='+',
        metavar="MODEL=PATH[,LORA_WEIGHTS=PATH]",
        required=True,
        help="multiple key-value pairs of model names and paths/urls to models/adapters, with optional LoRA weights (e.g., MODEL=path,LORA_WEIGHTS=path)",
    )
    return argument_parser.parse_args()

# https://stackoverflow.com/a/54802737
def chunk(l, n):
    """Yield n number of striped chunks from l."""
    for i in range(0, n):
        yield l[i::n]

def interleave(chunks):
    """Interleave chunks until one is exhausted."""
    interleaved = list()
    for idx in count():
        try:
            interleaved.extend(chunk[idx] for chunk in chunks)
        except IndexError:
            break
    return interleaved

def generate(pipe, image, caption, strict=False, timeout=None, **tqdm_kwargs):
    """Run MCTS until the generated tikz code compiles."""
    start, success, tikzpics = time(), False, set()
    for score, tikzpic in tqdm(pipe.simulate(image=image, caption=caption), desc="Try", **tqdm_kwargs):
        tikzpics.add((score, tikzpic))
        if not tikzpic.compiled_with_errors if strict else tikzpic.is_rasterizable:
            success = True
        if success and (not timeout or time() - start >= timeout):
            break
    return [tikzpic for _, tikzpic in sorted(tikzpics, key=itemgetter(0))]

def predict(model_name, base_model, testset, cache_file=None, timeout=None, key="image", lora_weights_path=None):
    predictions, worker_preds = list(), list()
    if lora_weights_path:
        model, tokenizer = load_lora(
            base_model=base_model,
            lora_weights_path=lora_weights_path,
            device_map=RANK,
            torch_dtype=bfloat16 if is_cuda_available() and is_bf16_supported() else float16,
            attn_implementation="flash_attention_2" if is_flash_attn_2_available() else None,
        )
    else:
        model, tokenizer = load_model(
            base_model=base_model,
            lora_training=False,
            device_map=RANK,
            torch_dtype=bfloat16 if is_cuda_available() and is_bf16_supported() else float16,
            attn_implementation="flash_attention_2" if is_flash_attn_2_available() else None,
        )
    # if we don't have a timeout (i.e., only run mcts until we obtain smth compileable), we can use fast metrics
    metric_type = "model" if timeout else "fast"
    pipe = DetikzifyPipeline(model=model, tokenizer=tokenizer, metric=metric_type)
    if cache_file and isfile(cache_file):
        with open(cache_file) as f:
            # disable timeout as we know that the (last) images compile
            predictions = [[TikzDocument(code, timeout=None) for code in sample] for sample in load_json(f)]
    try:
        worker_chunk = list(chunk(list(testset)[len(predictions):], WORLD_SIZE))[RANK]
        # FIXME: right now there only is a progress bar for Rank 0
        for item in tqdm(worker_chunk, desc=f"{model_name.title()} ({RANK})", disable=RANK!=0):
            tikz = generate(pipe, image=item[key], caption = item["caption"], timeout=timeout, position=1, leave=False, disable=RANK!=0)
            worker_preds.append(tikz)
        del model, tokenizer, pipe
    finally:
        dist.all_gather_object(gathered:=WORLD_SIZE * [None], worker_preds)
        predictions.extend(interleave(gathered))
        if cache_file and RANK == 0:
            with open(cache_file, 'w') as f:
                dump([[p.code for p in ps] for ps in predictions], f)
    return predictions

def load_metrics(trainset, measure_throughput=False, **kwargs):
    bleu = CrystalBLEU(corpus=trainset, **kwargs)
    eed = TexEditDistance(**kwargs)
    emdsim = ImageSim(mode="emd", **kwargs)
    cossim = ImageSim(**kwargs)
    dreamsim = DreamSim(**kwargs)
    kid = KernelInceptionDistance(**kwargs)

    def mean_token_efficiency(predictions, limit=0.05):
        samples = list()
        for preds in predictions:
            samples.append(len(preds[-1].code)/sum(len(pred.code) for pred in preds))
        return winsorize(array(samples), limits=limit).mean().item()

    def mean_sampling_throughput(predictions, limit=0.05):
        return winsorize(array(list(map(len, predictions))), limits=limit).mean().item()

    def compute(references, predictions):
        ref_code, pred_code = [[ref['code']] for ref in references], [pred[-1].code for pred in predictions]
        ref_image, pred_image = [ref['image'] for ref in references], [pred[-1].rasterize() for pred in predictions]
        assert all(pred[-1].is_rasterizable for pred in predictions)

        if measure_throughput:
            scores = {"MeanSamplingThroughput": mean_sampling_throughput(predictions=predictions)}
        else:
            scores = {"MeanTokenEfficiency": mean_token_efficiency(predictions=predictions)}

        metrics = {
            bleu: partial(bleu.update, list_of_references=ref_code, hypotheses=pred_code),
            eed: partial(eed.update, target=ref_code, preds=pred_code),
            emdsim: lambda: [emdsim.update(img1=img1, img2=img2) for img1, img2 in zip(ref_image, pred_image)],
            cossim: lambda: [cossim.update(img1=img1, img2=img2) for img1, img2 in zip(ref_image, pred_image)],
            dreamsim: lambda: [dreamsim.update(img1=img1, img2=img2) for img1, img2 in zip(ref_image, pred_image)],
            kid: lambda: [(kid.update(img1, True), kid.update(img2, False)) for img1, img2 in zip(ref_image, pred_image)],
        }

        for metric, update in metrics.items():
            update()
            scores[str(metric)] = metric.compute() # type: ignore
            metric.reset()
        return scores

    return compute

if __name__ == "__main__":
    set_seed(0)
    dist.init_process_group(timeout=timedelta(days=3))
    args = parse_args()

    trainset = load_dataset("parquet", data_files=args.trainset, split="train")
    testset = load_dataset("parquet", data_files={"test": args.testset}, split="test").sort("caption") # type: ignore

    predictions = defaultdict(list)
       
    for item in tqdm(args.path, desc="Predicting"):
        # Split the model argument string into parts
        model_parts = dict(part.split('=') for part in item.split(',') if '=' in part)
        model_name = model_parts.get("MODEL")
        path = model_parts.get("PATH")
        lora_weights = model_parts.get("LORA_WEIGHTS", None)

        if path.endswith("json"):
            with open(path) as f:
                predictions[model_name] = [[TikzDocument(code, None) for code in sample] for sample in load_json(f)]
        else:
            cache_file = join(args.cache_dir, f'{model_name}.json') if args.cache_dir else None

            if lora_weights:
                 predictions[model_name] = predict(
                    model_name=model_name,
                    base_model=path,
                    testset=testset,
                    cache_file=cache_file,
                    timeout=args.timeout,
                    key="sketch" if args.use_sketches else "image",
                    lora_weights_path=lora_weights
                )
            else: 
                predictions[model_name] = predict(
                    model_name=model_name,
                    base_model=path,
                    testset=testset,
                    cache_file=cache_file,
                    timeout=args.timeout,
                    key="sketch" if args.use_sketches else "image"
                )

    if RANK == 0: # Scoring only on main process
        scores = dict()
        metrics = load_metrics(trainset['code'], measure_throughput=args.timeout is not None, sync_on_compute=False) # type: ignore
        for model_name, prediction in tqdm(predictions.items(), desc="Computing metrics", total=len(predictions)):
            scores[model_name] = metrics(references=testset, predictions=prediction)
        with open(args.output, "w") as file:
            dump(scores, file)
