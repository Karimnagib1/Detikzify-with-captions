from datasets import DownloadManager
from transformers import AutoConfig, AutoModel
from transformers import AutoTokenizer, PretrainedConfig
from transformers.utils.hub import is_remote_url
from peft import LoraConfig, get_peft_model, PeftConfig, PeftModel


from .detikzify import DetikzifyConfig, DetikzifyForCausalLM, DetikzifyTokenizer

def register():
    try:
        AutoConfig.register("detikzify", DetikzifyConfig)
        AutoModel.register(DetikzifyConfig, DetikzifyForCausalLM)
    except ValueError:
        pass # already registered

def load_deepseek(size="1.3b", **kwargs):
    return load(
        base_model=f"deepseek-ai/deepseek-coder-{size}-base{'-v1.5' if size == '7b' else ''}",
        **kwargs
    )

def load_codellama(size="7b", **kwargs):
    return load(
        base_model=f"codellama/CodeLlama-{size}-hf",
        **kwargs
    )

def load(base_model, vision_tower="vit_so400m_patch14_siglip_384.webli", pretrain_mm_mlp_adapter=None, lora_r=16, lora_alpha=32, lora_training=True, **kwargs):
    base_tokenizer = PretrainedConfig.from_pretrained(base_model).name_or_path or base_model
    tokenizer = AutoTokenizer.from_pretrained(
        pretrained_model_name_or_path=base_tokenizer,
        model_max_length=2048,
        add_bos_token=False,
        add_eos_token=True,
        pad_token="<pad>",
        padding_side="right", # Note: only for training, need to change to "left" for batched inference
        legacy=False
    )
    model = DetikzifyForCausalLM.from_pretrained(
        pretrained_model_name_or_path=base_model,
        use_cache=True,
        **kwargs
    )
    model.config.model_type = DetikzifyConfig.model_type # type: ignore
    model.generation_config.pad_token_id = tokenizer.pad_token_id # type: ignore

    if len(tokenizer) > model.config.vocab_size: # type: ignore
        model.resize_token_embeddings(len(tokenizer), pad_to_multiple_of=8) # type: ignore
    if pretrain_mm_mlp_adapter and is_remote_url(pretrain_mm_mlp_adapter):
        pretrain_mm_mlp_adapter = DownloadManager().download(pretrain_mm_mlp_adapter)

    processor = model.get_model().initialize_vision_modules( # type: ignore
        patch_token_id=tokenizer.bos_token_id,
        pretrain_mm_mlp_adapter=pretrain_mm_mlp_adapter,
        vision_tower=getattr(model.config, "vision_tower", vision_tower), # type: ignore
        feature_layer=getattr(model.config, "feature_layer", -1), # type: ignore
        concat_patches=getattr(model.config, "concat_patches", 2) # type: ignore
    )
    if lora_training:
        lora_config = LoraConfig(
                r=lora_r,
                lora_alpha=lora_alpha,
                target_modules=["q_proj", "v_proj"],  # Adjust target modules as needed
                lora_dropout=0.1,  # Adjust dropout as needed
                task_type="CAUSAL_LM"  # For language modeling
            )
        model = get_peft_model(model, lora_config)

    return model, DetikzifyTokenizer(text=tokenizer, image=processor)



def load_lora(base_model, vision_tower="vit_so400m_patch14_siglip_384.webli", pretrain_mm_mlp_adapter=None, lora_weights_path=None, **kwargs):
    # Load the tokenizer
    base_tokenizer = PretrainedConfig.from_pretrained(base_model).name_or_path or base_model
    tokenizer = AutoTokenizer.from_pretrained(
        pretrained_model_name_or_path=base_tokenizer,
        model_max_length=2048,
        add_bos_token=False,
        add_eos_token=True,
        pad_token="<pad>",
        padding_side="right", # Note: only for training, need to change to "left" for batched inference
        legacy=False
    )

    # Load the base model
    model = DetikzifyForCausalLM.from_pretrained(
        pretrained_model_name_or_path=base_model,
        use_cache=True,
        **kwargs
    )

    # Load and apply LoRA weights if provided
    if lora_weights_path:
        peft_config = PeftConfig.from_pretrained(lora_weights_path)
        model = PeftModel.from_pretrained(model, lora_weights_path)

    # Set model configuration
    model.config.model_type = DetikzifyConfig.model_type  # type: ignore
    model.generation_config.pad_token_id = tokenizer.pad_token_id  # type: ignore

    # Resize token embeddings if tokenizer size exceeds model vocab size
    if len(tokenizer) > model.config.vocab_size:  # type: ignore
        model.resize_token_embeddings(len(tokenizer), pad_to_multiple_of=8)  # type: ignore

    # Download and load the vision module if necessary
    if pretrain_mm_mlp_adapter and is_remote_url(pretrain_mm_mlp_adapter):
        pretrain_mm_mlp_adapter = DownloadManager().download(pretrain_mm_mlp_adapter)

    processor = model.get_model().initialize_vision_modules(  # type: ignore
        patch_token_id=tokenizer.bos_token_id,
        pretrain_mm_mlp_adapter=pretrain_mm_mlp_adapter,
        vision_tower=getattr(model.config, "vision_tower", vision_tower),  # type: ignore
        feature_layer=getattr(model.config, "feature_layer", -1),  # type: ignore
        concat_patches=getattr(model.config, "concat_patches", 2)  # type: ignore
    )

    return model, DetikzifyTokenizer(text=tokenizer, image=processor)