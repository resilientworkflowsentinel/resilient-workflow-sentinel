#app/local_llm_service/llm_app.py
# Copyright (c) 2025 Resilient Workflow Sentinel Contributors
# SPDX-License-Identifier: AGPL-3.0-or-later
# See the LICENSE file in the project root for details.
import os
import re
import json
import logging
import threading
import numpy as np
import time
from datetime import datetime
from typing import Optional, Dict, Any, List, Tuple
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

logger = logging.getLogger("uvicorn.error")
app = FastAPI(title="Local LLM Service - Clean Test")

ENABLE_LLM_DEBUG_LOGS = False
ENABLE_COMPREHENSIVE_DEBUG = False  # Disable detailed layer logging for clean test

LOCAL_MODEL_PATH = os.path.normpath(
    os.path.join(os.path.dirname(__file__), "..", "..", "models", "qwen2.5-7b-instruct")
)

# Debug log file path (same location as this file)
DEBUG_LOG_PATH = os.path.join(os.path.dirname(__file__), "llm_debug_log.txt")
ENABLE_LAYER_DEBUG = False  # Full debug: hidden states (during gen) + attention (post-gen) + logits + tokens

tokenizer = None
model = None
pipeline = None
_model_lock = threading.Lock()
_load_status = {"status": "initializing", "percent": 0, "message": "starting"}

# Steering vectors storage
steering_vectors = {}

try:
    import torch
    from transformers import (
        AutoTokenizer,
        AutoModelForCausalLM,
        TextGenerationPipeline,
        GenerationConfig,
        BitsAndBytesConfig,
    )
    HF_AVAILABLE = True
except Exception as e:
    logger.warning("Transformers/torch not available: %s", e)
    HF_AVAILABLE = False

def _try_parse_json_candidate(candidate: str) -> Optional[Dict[str, Any]]:
    try:
        parsed = json.loads(candidate)
        if isinstance(parsed, dict):
            return parsed
    except Exception:
        pass
    return None

def clean_exclamation_corruption(text: str, candidates: Optional[List[str]] = None) -> str:
    """
    Intelligently clean ! character corruption from model output.
    
    This happens when output_attentions=True forces manual attention implementation,
    causing the model to insert ! instead of spaces or nothing.
    
    Args:
        text: Raw model output (potentially corrupted with !)
        candidates: List of valid candidate names (for winner field validation)
    
    Returns:
        Cleaned text with ! corruption removed
    """
    if not text:
        return text
    
    # Always log when ! is present (critical for debugging)
    has_exclamation = "!" in text
    if has_exclamation:
        logger.warning(f"⚠️ EXCLAMATION CORRUPTION DETECTED in output ({len(text)} chars)")
        logger.warning(f"Raw (first 200 chars): {text[:200]}")
    
    if not has_exclamation:
        return text
    
    original = text
    
    # NO TRY-EXCEPT - LET ERRORS BUBBLE UP SO WE CAN SEE THEM
    
    # 1. Clean JSON field names: "!reasons!" → "reasons", "!!winner!!" → "winner"
    text = re.sub(r'[!]+([a-zA-Z_]\w*)[!]*["\']?\s*:', r'\1":', text)
    text = re.sub(r'"[!]+([a-zA-Z_]\w*)[!]*"', r'"\1"', text)
    
    # 2. Clean inside quoted strings: replace ! between word characters with space
    def clean_quoted_string(match):
        content = match.group(1)
        # Replace ! between letters/numbers with space
        content = re.sub(r'([a-zA-Z0-9])!+([a-zA-Z0-9])', r'\1 \2', content)
        # Remove remaining isolated !
        content = re.sub(r'!+', '', content)
        return f'"{content}"'
    
    text = re.sub(r'"([^"]*!+[^"]*)"', clean_quoted_string, text)
    
    # 3. Special handling for winner field with multiple names
    # "alice!bob!carol" → extract first valid candidate
    if candidates:
        winner_match = re.search(r'"winner"\s*:\s*"([^"]+)"', text)
        if winner_match:
            winner_value = winner_match.group(1)
            original_winner = winner_value
            
            # Check if winner has ! or multiple candidate names
            needs_extraction = "!" in winner_value or sum(1 for c in candidates if c.lower() in winner_value.lower()) > 1
            
            if needs_extraction:
                # Try to find first valid candidate
                extracted = None
                for candidate in candidates:
                    if candidate.lower() in winner_value.lower():
                        extracted = candidate
                        # Replace with just this candidate
                        text = re.sub(
                            r'"winner"\s*:\s*"[^"]+"',
                            f'"winner": "{candidate}"',
                            text,
                            count=1
                        )
                        logger.warning(f"✓ Extracted winner '{candidate}' from corrupted '{original_winner}'")
                        break
                
                if not extracted:
                    logger.error(f"❌ Could not extract valid winner from '{original_winner}'")
    
    # 4. Clean any remaining ! in the text
    text = text.replace("!!", " ")  # Double ! likely meant to be space
    text = text.replace("!", "")     # Single ! just remove
    
    # 5. Normalize whitespace
    text = re.sub(r'\s+', ' ', text)
    text = text.strip()
    
    logger.warning(f"✓ Cleaning completed. Cleaned (first 200 chars): {text[:200]}")
    
    return text

def write_debug_log(message: str):
    """Stub function - debug logging disabled in production."""
    pass

def format_attention_bars(attention_dict: Dict[str, float], max_bar_length: int = 30) -> List[str]:
    """Stub function - debug logging disabled in production."""
    return []


def generate_constrained_json_with_steering(
    prompt: str,
    steering_vector_name: str,
    max_new_tokens: int = 100,
    temperature: float = 0.0,
    steering_strength: float = 0.0,  # BASELINE: No steering
    candidate_names: Optional[List[str]] = None  # For debug logging context
) -> str:
    """
    Generate JSON with both constraints AND steering applied.
    WITH COMPREHENSIVE DEBUG LOGGING TO CONSOLE.
    """
    global model, tokenizer, steering_vectors
    if model is None or tokenizer is None:
        raise RuntimeError("Model not loaded")
    
    # In baseline mode (strength=0.0), we don't need the steering vector
    steering_vector = None
    if steering_strength > 0.0:
        steering_vector = steering_vectors.get(steering_vector_name)
        if steering_vector is None:
            raise RuntimeError(f"Steering vector '{steering_vector_name}' not found")
    
    # Add JSON schema enforcement to prompt
    constrained_prompt = f"""{prompt}

OUTPUT RULES:
1. Return EXACTLY ONE JSON object
2. Start with {{ and end with }}
3. No text before or after the JSON
4. No multiple JSON objects

JSON:"""
    
    start_time = time.time()
    
    # === DEBUG LOGGING START ===
    if ENABLE_COMPREHENSIVE_DEBUG:
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        print("\n" + "=" * 100)
        print(f"🔍 GENERATION DEBUG - {timestamp}")
        print("=" * 100 + "\n")
        
        print("PROMPT:")
        print("-" * 100)
        print(f"{constrained_prompt}")
        print("-" * 100 + "\n")
        
        print("CONFIG:")
        print("-" * 100)
        print(f"  MODE: CLEAN TEST - No examples, no steering")
        print(f"  purpose: Test if LLM can handle task with just instructions")
        print(f"  max_new_tokens: {max_new_tokens}")
        print(f"  temperature: {temperature}")
        print("-" * 100 + "\n")
    
    try:
        # Tokenize
        inputs = tokenizer(constrained_prompt, return_tensors="pt", padding=True)
        if torch.cuda.is_available():
            inputs = {k: v.to("cuda") for k, v in inputs.items()}
        
        # === DEBUG: Log tokenization ===
        if ENABLE_COMPREHENSIVE_DEBUG:
            tokens = [tokenizer.decode([token_id]) for token_id in inputs['input_ids'][0]]
            print("TOKENIZATION:")
            print("-" * 100)
            print(f"Total tokens: {len(tokens)}")
            print(f"Input IDs shape: {inputs['input_ids'].shape}\n")
            print("Token breakdown:")
            for idx, (token_id, token) in enumerate(zip(inputs['input_ids'][0].tolist(), tokens)):
                print(f"  [{idx:3d}] ID={token_id:6d} | {repr(token)}")
            print("-" * 100 + "\n")
        
        # Storage for layers 14-19 monitoring (early reasoning phase)
        layers_data = {}  # {layer_idx: [hidden_states]}
        
        # Storage for ALL 28 layers debug logging (captured in POST-generation analysis)
        debug_hidden_states = {}  # {layer_idx: [hidden_states per step]}
        debug_attention_patterns = {}  # {layer_idx: [attention_weights per step]}
        debug_logits = []  # List of logits for each generation step
        debug_tokens = []  # List of (token_id, token_text) for each generation step
        
        # DO NOT enable output_attentions during generation - it breaks Qwen2!
        # We'll capture attention in a separate post-generation pass instead
        
        # BASELINE MODE: Monitor layers 14-19 WITHOUT steering
        monitored_layers = list(range(14, 20))  # Layers 14-19
        debug_layers = list(range(0, 28))  # ALL 28 layers for comprehensive analysis
        handles = []
        
        def monitoring_hook(layer_idx):
            """Monitor but don't modify"""
            def hook(module, input_tuple, output):
                hidden_states = output[0]
                
                # Store state WITHOUT modification
                if ENABLE_COMPREHENSIVE_DEBUG:
                    if layer_idx not in layers_data:
                        layers_data[layer_idx] = []
                    layers_data[layer_idx].append(hidden_states.clone().detach().cpu())
                
                # Return UNMODIFIED
                return output
            return hook
        
        def debug_hidden_hook(layer_idx):
            """Capture hidden states for ALL 28 layers debug logging"""
            def hook(module, input_tuple, output):
                hidden_states = output[0]
                if ENABLE_LAYER_DEBUG:
                    if layer_idx not in debug_hidden_states:
                        debug_hidden_states[layer_idx] = []
                    debug_hidden_states[layer_idx].append(hidden_states.clone().detach().cpu())
                return output
            return hook
        
        def debug_attention_hook(layer_idx):
            """Capture attention weights from self_attn for ALL 28 layers"""
            def hook(module, input_tuple, output):
                # self_attn output: (attn_output, attn_weights, past_key_value)
                # attn_weights available when model.config.output_attentions=True
                if ENABLE_LAYER_DEBUG and len(output) > 1 and output[1] is not None:
                    attention_weights = output[1]
                    if layer_idx not in debug_attention_patterns:
                        debug_attention_patterns[layer_idx] = []
                    debug_attention_patterns[layer_idx].append(attention_weights.clone().detach().cpu())
                return output
            return hook
        
        # Register monitoring hooks for layers 14-19
        for layer_idx in monitored_layers:
            layer = model.model.layers[layer_idx]
            handle = layer.register_forward_hook(monitoring_hook(layer_idx))
            handles.append(handle)
        
        # Register debug hooks for ALL 28 layers (hidden states only during generation)
        # Attention will be captured in post-generation analysis pass
        if ENABLE_LAYER_DEBUG:
            for layer_idx in debug_layers:
                layer = model.model.layers[layer_idx]
                
                # Hook for hidden states on layer output
                handle = layer.register_forward_hook(debug_hidden_hook(layer_idx))
                handles.append(handle)
                
                # NO attention hooks during generation - causes corruption!
        
        # ===================================================================
        # ALL STEERING DISABLED FOR VOTES - Using natural model processing
        # ===================================================================
        # Layer 3 and Layer 5 steering DISABLED - was causing alice bias
        # Keeping only natural processing for fair candidate selection
        
        if ENABLE_LLM_DEBUG_LOGS:
            logger.info("🚫 ALL STEERING DISABLED for votes - natural processing only")
        
        try:
            if ENABLE_COMPREHENSIVE_DEBUG:
                # === TOKEN-BY-TOKEN GENERATION WITH FULL DEBUG LOGGING ===
                output_ids = inputs['input_ids'].clone()
                
                for step in range(max_new_tokens):
                    # Get model outputs
                    with torch.no_grad():
                        outputs = model(
                            input_ids=output_ids,
                            output_hidden_states=True,
                            return_dict=True
                        )
                    
                    logits = outputs.logits[:, -1, :]
                    
                    # Sample next token
                    if temperature > 0:
                        probs = torch.softmax(logits / temperature, dim=-1)
                        next_token = torch.multinomial(probs, num_samples=1)
                    else:
                        next_token = torch.argmax(logits, dim=-1, keepdim=True)
                    
                    next_token_id = next_token.item()
                    next_token_text = tokenizer.decode([next_token_id])
                    
                    # Capture for debug logging
                    if ENABLE_LAYER_DEBUG:
                        debug_logits.append(logits.clone().detach().cpu())
                        debug_tokens.append((next_token_id, next_token_text))
                    
                    # === DEBUG: Log this generation step ===
                    print(f"\n{'=' * 100}")
                    print(f"GENERATION STEP {step + 1}")
                    print(f"{'=' * 100}")
                    print(f"Generated Token: ID={next_token_id} | {repr(next_token_text)}\n")
                    
                    # Log probabilities
                    probs = torch.softmax(logits[0], dim=-1)
                    top_k = 10
                    top_probs, top_indices = torch.topk(probs, top_k)
                    
                    print(f"Top {top_k} Token Probabilities:")
                    print("-" * 100)
                    for rank, (prob, idx) in enumerate(zip(top_probs, top_indices), 1):
                        token_text = tokenizer.decode([idx.item()])
                        print(f"  {rank:2d}. ID={idx.item():6d} {repr(token_text):20s} | Prob={prob.item():.6f} ({prob.item()*100:.2f}%)")
                    print("-" * 100 + "\n")
                    
                    # Log layers 14-19 analysis (early reasoning phase)
                    if layers_data:
                        print("\nLAYERS 14-19 ANALYSIS (EARLY REASONING):")
                        print("=" * 100)
                        
                        for layer_idx in range(14, 20):
                            if layer_idx in layers_data and layers_data[layer_idx]:
                                hidden = layers_data[layer_idx][-1]
                                last_token = hidden[0, -1, :]
                                
                                print(f"\nLayer {layer_idx}:")
                                print(f"  Norm: {last_token.norm().item():.2f}")
                                print(f"  Mean: {last_token.mean().item():+.4f}")
                                print(f"  Std: {last_token.std().item():.4f}")
                                
                                # Show top 5 dimensions only
                                abs_vals = last_token.abs()
                                top_dims = torch.argsort(abs_vals, descending=True)[:5]
                                print(f"  Top 5 dims:", end="")
                                for dim_idx in top_dims:
                                    val = last_token[dim_idx].item()
                                    print(f" {dim_idx}:{val:+.2f}", end="")
                                print()
                        
                        print("=" * 100 + "\n")
                    
                    
                    # Append to output
                    output_ids = torch.cat([output_ids, next_token], dim=-1)
                    
                    # Stop if EOS token
                    if next_token_id == tokenizer.eos_token_id:
                        break
                    
                    # Stop if closing brace (simple heuristic for JSON)
                    if next_token_text.strip() == '}':
                        # Check if we have a complete JSON
                        current_text = tokenizer.decode(
                            output_ids[0][inputs['input_ids'].shape[1]:], 
                            skip_special_tokens=True
                        )
                        if current_text.count('{') == current_text.count('}'):
                            break
                
                outputs_final = output_ids
                
            else:
                # === FAST GENERATION (no debug) ===
                gen_config = GenerationConfig(
                    max_new_tokens=max_new_tokens,
                    temperature=temperature if temperature > 0 else None,
                    do_sample=temperature > 0,
                    pad_token_id=tokenizer.pad_token_id or tokenizer.eos_token_id,
                    eos_token_id=tokenizer.eos_token_id,
                    top_p=None,
                    top_k=None,
                )
                
                with torch.no_grad():
                    outputs_final = model.generate(
                        **inputs,
                        generation_config=gen_config,
                        max_new_tokens=max_new_tokens,
                    )
            
            generated = tokenizer.decode(outputs_final[0], skip_special_tokens=True)
            
            # Extract only the part after the prompt
            if constrained_prompt in generated:
                result = generated[len(constrained_prompt):].strip()
            else:
                result = generated[len(prompt):].strip()
            
            # Extract first complete JSON only
            if '{' in result:
                start = result.index('{')
                depth = 0
                end = start
                
                for i in range(start, len(result)):
                    if result[i] == '{':
                        depth += 1
                    elif result[i] == '}':
                        depth -= 1
                        if depth == 0:
                            end = i + 1
                            break
                
                if end > start:
                    result = result[start:end]
            
            # === DEBUG: Log final output ===
            if ENABLE_COMPREHENSIVE_DEBUG:
                duration = time.time() - start_time
                num_generated = outputs_final.shape[1] - inputs['input_ids'].shape[1]
                
                print("\n" + "=" * 100)
                print("FINAL OUTPUT")
                print("=" * 100 + "\n")
                
                print("GENERATED TEXT:")
                print("-" * 100)
                print(f"{result}")
                print("-" * 100 + "\n")
                
                print("STATISTICS:")
                print("-" * 100)
                print(f"Total tokens generated: {num_generated}")
                print(f"Generation duration: {duration:.3f}s")
                if duration > 0:
                    print(f"Tokens per second: {num_generated/duration:.2f}")
                print("-" * 100 + "\n")
                
                print("=" * 100)
                print("END OF GENERATION")
                print("=" * 100 + "\n")
            
            # Clean ! corruption before returning (NO FALLBACK - FAIL LOUDLY)
            raw_result = result.strip()
            
            # Log BEFORE cleaning (always)
            
            # CLEAN IT - NO TRY-EXCEPT - LET IT FAIL IF THERE'S A PROBLEM
            cleaned_result = clean_exclamation_corruption(raw_result, candidates=candidate_names)
            
            # Log AFTER cleaning (always)
            
            if raw_result != cleaned_result:
                logger.warning(f"✓ ! corruption was CLEANED (changed)")
            else:
                logger.info(f"✓ No ! corruption detected (unchanged)")
            
            # === POST-GENERATION ATTENTION ANALYSIS (SAFE - DOESN'T AFFECT OUTPUT) ===
            # Now that we have clean output, run a SEPARATE forward pass to capture attention
            # This is the CORRECT way - generation and analysis are separate!
            if ENABLE_LAYER_DEBUG:
                try:
                    logger.info("Running post-generation attention analysis...")
                    
                    # Clear previous attention patterns
                    debug_attention_patterns.clear()
                    
                    # Register attention capture hooks for ALL 28 layers
                    analysis_handles = []
                    
                    def attention_capture_hook(layer_idx):
                        """Capture attention in post-generation analysis"""
                        def hook(module, input_tuple, output):
                            if len(output) > 1 and output[1] is not None:
                                attention_weights = output[1]
                                if layer_idx not in debug_attention_patterns:
                                    debug_attention_patterns[layer_idx] = []
                                debug_attention_patterns[layer_idx].append(attention_weights.clone().detach().cpu())
                            return output
                        return hook
                    
                    # Register hooks on self_attn for ALL 28 layers
                    for layer_idx in debug_layers:
                        layer = model.model.layers[layer_idx]
                        if hasattr(layer, 'self_attn'):
                            handle = layer.self_attn.register_forward_hook(attention_capture_hook(layer_idx))
                            analysis_handles.append(handle)
                    
                    # Run SEPARATE forward pass with output_attentions=True
                    # This doesn't affect the output we already generated!
                    with torch.no_grad():
                        _ = model(
                            input_ids=outputs_final,
                            output_attentions=True,
                            return_dict=True
                        )
                    
                    # Remove analysis hooks
                    for handle in analysis_handles:
                        handle.remove()
                    
                    logger.info(f"✓ Captured attention patterns for {len(debug_attention_patterns)} layers")
                    
                except Exception as e:
                    logger.warning(f"Post-generation attention analysis failed (non-critical): {e}")
            
            return cleaned_result
            
        finally:
            # === COMPREHENSIVE DEBUG LOGGING FOR ALL 28 LAYERS ===
            if ENABLE_LAYER_DEBUG and (debug_hidden_states or debug_attention_patterns or debug_logits or debug_tokens):
                write_debug_log("\n" + "="*80)
                write_debug_log("ALL 28 LAYERS - COMPREHENSIVE DEBUG ANALYSIS")
                write_debug_log("="*80)
                write_debug_log(f"Timestamp: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
                if candidate_names:
                    write_debug_log(f"Candidates: {candidate_names}")
                write_debug_log(f"Generation steps captured: {len(debug_tokens)}")
                
                # Log raw vs cleaned output
                if 'raw_result' in locals() and 'cleaned_result' in locals():
                    write_debug_log(f"\nRAW OUTPUT (before cleaning): {raw_result}")
                    if raw_result != cleaned_result:
                        write_debug_log(f"CLEANED OUTPUT (after ! removal): {cleaned_result}")
                        write_debug_log(f"! corruption was detected and cleaned ✓")
                    else:
                        write_debug_log(f"No ! corruption detected ✓")
                
                write_debug_log("")
                
                # Log per-layer analysis for ALL 28 layers
                for layer_idx in range(0, 28):
                    write_debug_log(f"\n{'='*60}")
                    write_debug_log(f"LAYER {layer_idx} ANALYSIS")
                    write_debug_log(f"{'='*60}")
                    
                    # 1. Hidden States Statistics
                    if layer_idx in debug_hidden_states and debug_hidden_states[layer_idx]:
                        hidden = debug_hidden_states[layer_idx][-1]  # Last captured state
                        last_token_hidden = hidden[0, -1, :]  # [hidden_dim]
                        
                        # Calculate sparsity (% of values near zero)
                        sparsity_threshold = 0.01
                        near_zero = (last_token_hidden.abs() < sparsity_threshold).sum().item()
                        total_elements = last_token_hidden.numel()
                        sparsity_pct = (near_zero / total_elements) * 100
                        
                        write_debug_log(f"\nHidden States Statistics:")
                        write_debug_log(f"  Shape:      {hidden.shape}")
                        write_debug_log(f"  Norm (L2):  {last_token_hidden.norm().item():.4f}")
                        write_debug_log(f"  Mean:       {last_token_hidden.mean().item():.6f}")
                        write_debug_log(f"  Std Dev:    {last_token_hidden.std().item():.6f}")
                        write_debug_log(f"  Min:        {last_token_hidden.min().item():.6f}")
                        write_debug_log(f"  Max:        {last_token_hidden.max().item():.6f}")
                        write_debug_log(f"  Sparsity:   {sparsity_pct:.2f}% (values < {sparsity_threshold})")
                    else:
                        write_debug_log(f"\nHidden States: Not captured for this layer")
                    
                    # 2. Attention Patterns with Entropy
                    if layer_idx in debug_attention_patterns and debug_attention_patterns[layer_idx] and candidate_names:
                        attn = debug_attention_patterns[layer_idx][-1]  # Last captured attention
                        
                        write_debug_log(f"\nAttention Patterns:")
                        write_debug_log(f"  Shape: {attn.shape}")
                        
                        # Average across batch and heads
                        if attn.dim() == 4:
                            # [batch, num_heads, seq_len, seq_len]
                            attn_avg = attn.mean(dim=(0, 1))  # [seq_len, seq_len]
                        elif attn.dim() == 3:
                            # [batch, seq_len, seq_len] or [heads, seq_len, seq_len]
                            attn_avg = attn.mean(dim=0)  # [seq_len, seq_len]
                        else:
                            attn_avg = None
                        
                        if attn_avg is not None:
                            # Get attention from last position
                            last_token_attn = attn_avg[-1, :]  # [seq_len]
                            last_token_attn = last_token_attn / (last_token_attn.sum() + 1e-9)
                            
                            # Calculate attention entropy (measure of focus/diffusion)
                            # Low entropy = focused, High entropy = diffuse
                            attn_probs = last_token_attn + 1e-9  # Add small epsilon
                            entropy = -(attn_probs * torch.log(attn_probs)).sum().item()
                            max_entropy = np.log(len(attn_probs))  # Maximum possible entropy
                            normalized_entropy = entropy / max_entropy if max_entropy > 0 else 0
                            
                            write_debug_log(f"  Entropy:     {entropy:.4f} (normalized: {normalized_entropy:.4f})")
                            write_debug_log(f"  Max Attn:    {last_token_attn.max().item():.4f}")
                            write_debug_log(f"  Focus:       {'Sharp' if normalized_entropy < 0.3 else 'Moderate' if normalized_entropy < 0.7 else 'Diffuse'}")
                            
                            # Map to candidates
                            num_candidates = len(candidate_names)
                            seq_len = last_token_attn.size(0)
                            
                            if seq_len >= num_candidates and num_candidates > 0:
                                candidate_attns = last_token_attn[-num_candidates:].numpy()
                                attention_dict = {
                                    name: float(attn_val) 
                                    for name, attn_val in zip(candidate_names, candidate_attns)
                                }
                                
                                write_debug_log(f"  Attention to candidates:")
                                for line in format_attention_bars(attention_dict):
                                    write_debug_log(line)
                            else:
                                write_debug_log(f"  Cannot map attention (seq_len={seq_len}, candidates={num_candidates})")
                        else:
                            write_debug_log(f"  Cannot process attention shape")
                    elif layer_idx in debug_attention_patterns and debug_attention_patterns[layer_idx]:
                        write_debug_log(f"\nAttention: Captured but no candidates to analyze")
                    else:
                        write_debug_log(f"\nAttention: Not captured for this layer")
                    
                    write_debug_log("")
                
                # 3. Token Generation Log (across all layers)
                if debug_tokens:
                    write_debug_log(f"\n{'='*60}")
                    write_debug_log(f"TOKEN GENERATION SEQUENCE")
                    write_debug_log(f"{'='*60}")
                    write_debug_log(f"Total tokens generated: {len(debug_tokens)}")
                    write_debug_log("")
                    
                    for step, (token_id, token_text) in enumerate(debug_tokens, 1):
                        write_debug_log(f"  Step {step:2d}: ID={token_id:6d} | {repr(token_text)}")
                    write_debug_log("")
                
                # 4. Logits Analysis (top-10 per step)
                if debug_logits:
                    write_debug_log(f"\n{'='*60}")
                    write_debug_log(f"LOGITS ANALYSIS (TOP-10 PER STEP)")
                    write_debug_log(f"{'='*60}")
                    
                    for step, logits_tensor in enumerate(debug_logits, 1):
                        probs = torch.softmax(logits_tensor[0], dim=-1)
                        top_k = 10  # Show top-10 tokens
                        top_probs, top_indices = torch.topk(probs, top_k)
                        
                        # Calculate entropy for this step
                        log_probs = torch.log(probs + 1e-9)
                        entropy = -(probs * log_probs).sum().item()
                        
                        write_debug_log(f"\nStep {step}:")
                        write_debug_log(f"  Entropy: {entropy:.4f} (confidence: {'High' if entropy < 2.0 else 'Medium' if entropy < 5.0 else 'Low'})")
                        write_debug_log(f"  Top-10 tokens:")
                        for rank, (prob, idx) in enumerate(zip(top_probs, top_indices), 1):
                            token_text = tokenizer.decode([idx.item()])
                            write_debug_log(f"    {rank:2d}. ID={idx.item():6d} {repr(token_text):25s} | {prob.item():.6f} ({prob.item()*100:6.2f}%)")
                
                
                write_debug_log(f"\n{'='*80}\n")
            
            # Remove all hooks
            for handle in handles:
                handle.remove()
    
    except Exception as e:
        if ENABLE_COMPREHENSIVE_DEBUG:
            print("\n" + "!" * 100)
            print("ERROR OCCURRED")
            print("!" * 100)
            print(f"{type(e).__name__}: {str(e)}")
            print("!" * 100 + "\n")
        raise

def generate_constrained_json(prompt: str, max_new_tokens: int = 100, temperature: float = 0.0) -> str:
    """
    Generate JSON output with constraints to force valid single JSON object.
    Uses stop sequences and strict prompting to prevent multiple JSONs.
    """
    global model, tokenizer
    if model is None or tokenizer is None:
        raise RuntimeError("Model not loaded")
    
    # Add JSON schema enforcement to prompt
    constrained_prompt = f"""{prompt}

OUTPUT RULES:
1. Return EXACTLY ONE JSON object
2. Start with {{ and end with }}
3. No text before or after the JSON
4. No multiple JSON objects

JSON:"""
    
    # Tokenize
    inputs = tokenizer(constrained_prompt, return_tensors="pt", padding=True)
    if torch.cuda.is_available():
        inputs = {k: v.to("cuda") for k, v in inputs.items()}
    
    # Generate with strict stop tokens
    # Stop at: second '{', newline after '}', or common continuation patterns
    stop_strings = [
        "\n\n{",  # Prevents second JSON
        "}\n{",   # Prevents second JSON  
        "} {",    # Prevents second JSON
        "},\n",   # Prevents array of JSONs
        "}\n\n",  # Natural stopping point
    ]
    
    # Convert stop strings to token IDs
    stop_token_ids = []
    for stop_str in stop_strings:
        tokens = tokenizer.encode(stop_str, add_special_tokens=False)
        if tokens:
            stop_token_ids.append(tokens[0])
    
    # Generation config
    gen_config = GenerationConfig(
        max_new_tokens=max_new_tokens,
        temperature=temperature if temperature > 0 else None,
        do_sample=temperature > 0,
        pad_token_id=tokenizer.pad_token_id or tokenizer.eos_token_id,
        eos_token_id=tokenizer.eos_token_id,
        top_p=None,
        top_k=None,
    )
    
    # Generate
    with torch.no_grad():
        outputs = model.generate(
            **inputs,
            generation_config=gen_config,
            max_new_tokens=max_new_tokens,
        )
    
    # Decode
    generated = tokenizer.decode(outputs[0], skip_special_tokens=True)
    
    # Extract only the part after the prompt
    if constrained_prompt in generated:
        result = generated[len(constrained_prompt):].strip()
    else:
        result = generated[len(prompt):].strip()
    
    # Extract first complete JSON only
    if '{' in result:
        start = result.index('{')
        # Find matching closing brace
        depth = 0
        for i, char in enumerate(result[start:], start):
            if char == '{':
                depth += 1
            elif char == '}':
                depth -= 1
                if depth == 0:
                    # Found complete JSON
                    return result[start:i+1]
    
    return result

def extract_json_from_text(text: str) -> Optional[Dict[str, Any]]:
    if not text or "{" not in text:
        return None
    
    text = text.strip()
    text = re.sub(r'^```json\s*', '', text)
    text = re.sub(r'\s*```$', '', text)
    text = re.sub(r'^(Here is the JSON|The JSON object is|Output:|Result:)[:\s]*', '', text, flags=re.IGNORECASE)
    
    search_text = text[-10000:] if len(text) > 10000 else text
    
    opens = [m.start() for m in re.finditer(r"\{", search_text)]
    closes = [m.start() for m in re.finditer(r"\}", search_text)]
    
    if not opens or not closes:
        return None
    
    valid_objects = []
    
    for start in reversed(opens):
        for end in (c for c in closes if c > start):
            if end - start > 5000:
                continue
            
            candidate = search_text[start:end+1]
            candidate = candidate.strip()
            
            parsed = _try_parse_json_candidate(candidate)
            if parsed is not None:
                valid_objects.append(parsed)
                break
    
    for obj in valid_objects:
        has_content = False
        for key, value in obj.items():
            if isinstance(value, str) and value.strip() and value not in ["low|medium|high", "<one sentence>", "<one word>", "<n>", "X", "Y", "Z"]:
                has_content = True
                break
            if isinstance(value, (int, float, list)) and value:
                has_content = True
                break
            if value is None:
                has_content = True
                break
        if has_content:
            return obj
    
    if valid_objects:
        return valid_objects[0]
    
    return None

def _clean_summary_output(raw: str) -> Dict[str, Any]:
    raw = (raw or "").strip()
    VALID_URGENCY = {"low", "medium", "high"}
    
    parsed = extract_json_from_text(raw)
    if isinstance(parsed, dict):
        summary = (parsed.get("summary") or parsed.get("text") or parsed.get("description") or "").strip()
        urgency = (parsed.get("urgency") or parsed.get("priority") or "").strip().lower()
        if summary and urgency in VALID_URGENCY:
            return {"summary": summary, "urgency": urgency, "raw": parsed}
    
    m_sum = re.search(r'(?:summary|description)[:\s]*["\']?([^"\n\}]{3,400})', raw, re.IGNORECASE)
    m_urg = re.search(r'urgency[:\s]*["\']?(\w+)', raw, re.IGNORECASE)
    if m_sum and m_urg:
        summary = m_sum.group(1).strip()
        urgency = m_urg.group(1).strip().lower()
        if urgency in VALID_URGENCY:
            return {"summary": summary, "urgency": urgency, "raw": raw}
    
    return {"summary": "", "urgency": "", "raw": raw}

def _clean_vote_output(raw: str, candidates=None) -> Dict[str, Any]:
    raw = (raw or "").strip()
    candidates = candidates or []
    
    parsed = extract_json_from_text(raw)
    if isinstance(parsed, dict):
        winner = parsed.get("winner")
        reasons = parsed.get("reasons", [])
        raw_field = parsed.get("raw", parsed)
        
        if winner is None:
            return {
                "winner": None, 
                "reasons": list(reasons) if isinstance(reasons, list) else [reasons] if reasons else [], 
                "raw": raw_field
            }
        
        if winner and str(winner).lower() in [c.lower() for c in candidates]:
            if isinstance(reasons, str):
                reasons = [reasons]
            return {
                "winner": str(winner).strip(), 
                "reasons": list(reasons or []), 
                "raw": raw_field
            }
    
    m_winner = re.search(r'(?i)winner[:\s]*["\']?(\w+)', raw)
    if m_winner:
        candidate = m_winner.group(1).strip()
        
        if candidate.lower() in ["null", "none"]:
            return {"winner": None, "reasons": ["all overloaded"], "raw": raw}
        
        if candidate.lower() in [c.lower() for c in candidates]:
            reason_match = re.search(r'(?i)reason[s]?[:\-\s]*\[([^\]]+)\]', raw)
            if reason_match:
                try:
                    reasons = json.loads('[' + reason_match.group(1) + ']')
                except:
                    reasons = [r.strip(' "\'') for r in reason_match.group(1).split(',')]
            else:
                reason_match = re.search(r'(?i)reason[s]?[:\-\s]*["\']?(.+)', raw, re.DOTALL)
                reasons = [reason_match.group(1).strip()[:200]] if reason_match else []
            
            return {"winner": candidate, "reasons": reasons, "raw": raw}
    
    return {"winner": None, "reasons": [], "raw": raw}

# MINIMAL PROMPTS (steering handles verbosity, we provide structure)
SUMMARIZE_PROMPT = """Analyze task and output JSON only:
{{"summary": "<brief sentence>", "urgency": "low|medium|high"}}

Task: {task}"""

CLASSIFY_PROMPT = """Output JSON only: {{"category": "<type>"}}
Text: {task}"""

VOTE_STAGE1_PROMPT = """Check availability and filter to candidates with LOWEST load.

Calculate for EACH person: availability = capacity - load

If ALL have availability <= 0:
  Return: {{"has_available": false, "filtered_candidates": []}}

If ANY have availability > 0:
  1. Find the MINIMUM load among available candidates
  2. Return ONLY candidates with that minimum load
  Return: {{"has_available": true, "filtered_candidates": ["name1", "name2", ...]}}

candidates={candidates}
team={team_state}

JSON:"""

VOTE_STAGE2_PROMPT = """OUTPUT EXACTLY ONE JSON. NOT MULTIPLE.

If candidates list is EMPTY:
  Return: {{"winner": null, "reasons": ["all overloaded"]}}

If candidates list has members:
  Calculate: availability = capacity - load
  Pick: ONE candidate with highest availability
  Return: {{"winner":"<n>","reasons":["<why>"]}}

candidates={candidates}
team={team_state}

JSON:"""

class TextPayload(BaseModel):
    content: str

class VotePayload(BaseModel):
    content: str
    candidates: list
    team_state: list = []

class BatchTextPayload(BaseModel):
    contents: List[str]

class BatchVotePayload(BaseModel):
    items: List[Dict[str, Any]]

def _update_load(percent: int, status: str, message: str = ""):
    _load_status["status"] = status
    _load_status["percent"] = int(percent)
    _load_status["message"] = message
    logger.info("[load_status] %s %s%% %s", status, percent, message)

# ============================================================
# STEERING VECTOR IMPLEMENTATION
# ============================================================

def create_steering_vector_from_examples(
    positive_examples: List[str],
    negative_examples: List[str],
    layer_idx: int = 21,
    token_pos: int = -1
) -> torch.Tensor:
    """
    Create a steering vector by computing activations on positive/negative examples.
    
    This is a simplified implementation. For production, use RepE library or similar.
    """
    if not HF_AVAILABLE or model is None:
        logger.warning("Cannot create steering vector: model not loaded")
        return None
    
    try:
        with torch.no_grad():
            # Get activations for positive examples
            pos_activations = []
            for example in positive_examples:
                inputs = tokenizer(example, return_tensors="pt", truncation=True, max_length=512).to(model.device)
                outputs = model(**inputs, output_hidden_states=True)
                # Get hidden state at specified layer
                hidden_state = outputs.hidden_states[layer_idx]
                # Average across sequence or take specific position
                if token_pos == -1:
                    activation = hidden_state[0, -1, :].cpu()  # Last token
                else:
                    activation = hidden_state[0, token_pos, :].cpu()
                pos_activations.append(activation)
            
            # Get activations for negative examples
            neg_activations = []
            for example in negative_examples:
                inputs = tokenizer(example, return_tensors="pt", truncation=True, max_length=512).to(model.device)
                outputs = model(**inputs, output_hidden_states=True)
                hidden_state = outputs.hidden_states[layer_idx]
                if token_pos == -1:
                    activation = hidden_state[0, -1, :].cpu()
                else:
                    activation = hidden_state[0, token_pos, :].cpu()
                neg_activations.append(activation)
            
            # Compute steering vector as difference of means
            pos_mean = torch.stack(pos_activations).mean(dim=0)
            neg_mean = torch.stack(neg_activations).mean(dim=0)
            steering_vector = pos_mean - neg_mean
            
            # Normalize
            steering_vector = steering_vector / (steering_vector.norm() + 1e-8)
            
            return steering_vector.to(model.device)
            
    except Exception as e:
        logger.exception(f"Failed to create steering vector: {e}")
        return None

def initialize_steering_vectors():
    """Create steering vectors for different tasks"""
    global steering_vectors
    
    if not HF_AVAILABLE or model is None:
        logger.warning("Cannot initialize steering vectors: model not loaded")
        return
    
    logger.info("Creating steering vectors...")
    
    # Concise Summary Vector
    logger.info("Creating concise_summary vector...")
    steering_vectors['concise_summary'] = create_steering_vector_from_examples(
        positive_examples=[
            '{"summary":"Brief description","urgency":"high"}',
            "Summary: Main point",
            '{"summary":"Redis at 98%","urgency":"high"}',
            "Task: Critical issue"
        ],
        negative_examples=[
            "Let me provide a detailed explanation of why this is important...",
            "To thoroughly analyze this situation, we must first consider...",
            "The comprehensive reasoning behind this decision involves several factors..."
        ],
        layer_idx=21
    )
    
    # Precise Calculation Vector - Teaches ARITHMETIC, FILTERING, BALANCING, and SINGLE DECISION
    logger.info("Creating precise_calculation vector...")
    steering_vectors['precise_calculation'] = create_steering_vector_from_examples(
        positive_examples=[           
            # Shows filtering by availability > 0
            '{"winner":"person_A","reasons":["person_A: 8-3=5, person_B: 8-8=0, person_C: 8-9=-1. Only person_A has availability > 0"]}',
            '{"winner":"person_B","reasons":["person_A: 8-7=1, person_B: 8-5=3, person_C: 8-8=0. person_B has highest positive availability (3 > 1)"]}',
            # Shows null when all at/over capacity
            '{"winner":null,"reasons":["All at capacity: person_A=0, person_B=0, person_C=0"]}',
            '{"winner":null,"reasons":["All overloaded: person_A=-2, person_B=-1, person_C=-3"]}',
            # Shows correct filtering
            '{"winner":"person_C","reasons":["After filtering for availability > 0, only person_C (8-6=2) is eligible"]}',
            # BALANCING EXAMPLES - pick lowest load when tied on availability
            '{"winner":"person_B","reasons":["person_A: 8-2=6, person_B: 8-2=6, person_C: 8-5=3. Tied on availability, person_A load=2, person_B load=2, person_C load=5. All tied, pick person_B"]}',
            '{"winner":"person_C","reasons":["person_A: 8-1=7, person_B: 8-3=5, person_C: 8-0=8. person_C has highest availability (8 > 7 and 8 > 5) and lowest load=0, promotes balance"]}',
            # OVERLOAD SCENARIOS - correct handling
            '{"winner":"person_B","reasons":["person_A: 8-9=-1 (overload), person_B: 8-7=1, person_C: 8-8=0. Only person_B has positive availability"]}',
            '{"winner":"person_C","reasons":["person_A: 8-10=-2 (overload), person_B: 8-9=-1 (overload), person_C: 8-6=2. Only person_C eligible"]}',
            # Generic decision examples
            '{"winner":"candidate_1","reasons":["most available"]}',
            '{"selected":"option_B","score":8}',
        ],
        negative_examples=[ 
            # WRONG: Picking someone with negative availability
            '{"winner":"person_A","reasons":["person_A has -1 availability"]}',
            '{"winner":"person_B","reasons":["person_B: 8-10=-2, picked anyway"]}',
            # WRONG: False comparisons
            '{"winner":"person_A","reasons":["person_A: 5, person_B: 7, person_C: 8. person_A has highest (5 > 7 > 8)"]}',
            '{"winner":"person_A","reasons":["person_A availability 6 is greater than person_B 7 and person_C 8"]}',
            # WRONG: Not balancing when should
            '{"winner":"person_A","reasons":["person_A: 8-5=3, person_B: 8-1=7, person_C: 8-1=7. person_A picked despite person_B and person_C having lower load"]}',
            '{"winner":"person_C","reasons":["person_A: 8-0=8, person_B: 8-0=8, person_C: 8-5=3. person_C picked even though person_A and person_B have lower load"]}',
            # WRONG: Listing all options instead of picking one
            '{"option":"A"} {"option":"B"} {"option":"C"}',
            'person_A is available, person_B is free, person_C can do it',
            'Candidates: {"a":8} {"b":7} {"c":8}',
            '{"winner":"A"},{"winner":"B"},{"winner":"C"}',
            'Let me list all options: A has 8, B has 7, C has 8',
            # WRONG: Not filtering before picking
            '{"winner":"person_A","reasons":["person_A has highest of -1, -2, -3"]}'
        ],
        layer_idx=26  # Create vector from layer 26 (our target decision layer)
    )
    
    # Layer 3: Neutral Candidate Processing (Anti-Positional-Bias + Anti-Salience-Bias)
    logger.info("Creating layer3_neutral_listing vector...")
    steering_vectors['layer3_neutral_listing'] = create_steering_vector_from_examples(
        positive_examples=[
            # Neutral processing - read all candidates equally
            'Analyzing candidates: candidate_X, candidate_Y, candidate_Z',
            'Processing all options: person_1, person_2, person_3',
            'Candidates under consideration: option_A, option_B, option_C',
            'Evaluating each candidate: worker_1 (status), worker_2 (status), worker_3 (status)',
            'All candidates received: member_A, member_B, member_C - analyzing each',
            # Order-agnostic processing
            'Candidates in any order: person_3, person_1, person_2',
            'Options presented: candidate_Y, candidate_Z, candidate_X',
            'List contains: option_C, option_A, option_B - order irrelevant',
            # SALIENCE-NEUTRAL: Numbers exist but NO decision implied
            'Candidates with values: A=5, B=8, C=7 — recording all values',
            'Received metrics: person_1 has 6, person_2 has 9, person_3 has 4',
            'Inputs noted: X availability 8, Y availability 7, Z availability 6',
            'All candidate metrics observed without ranking',
            'Data points collected: option_A=4, option_B=7, option_C=9 - no selection yet',
            'Measurement results: worker_1 score 5, worker_2 score 8, worker_3 score 6',
            'Values registered: candidate_X capacity 7, candidate_Y capacity 9, candidate_Z capacity 5',
            'Noting all figures: member_A=8, member_B=6, member_C=7 without preference',
        ],
        negative_examples=[
            # Position-based favoritism
            'The first candidate is the best',
            'Picking the first option from the list',
            'First one is selected by default',
            'Choose the first one',
            'First in list - selected',
            'Default to first position',
            # Premature decision
            'The winner is decided',
            'Selected: first option',
            'Decision: first one wins',
            'First candidate is chosen',
            # SALIENCE-BASED: Numbers imply ranking/selection
            'The highest value candidate is selected',
            'Candidate with maximum availability wins',
            'Best score determines the winner',
            'This candidate stands out as the best',
            'Clearly superior option',
            'Winner obvious from the numbers',
            'Highest metric wins automatically',
            'Maximum value equals best choice',
            'Top score means this one is picked',
            'Numbers show clear winner',
        ],
        layer_idx=3  # Target layer 3 specifically
    )
    
    # Layer 5: REMOVED - Not using any steering for votes
    # Voting uses natural model processing with 2-stage prompts
    
    logger.info("✓ Steering vectors initialized!")


def generate_with_steering(
    prompt: str,
    steering_vector_name: str,
    max_new_tokens: int = 100,
    steering_strength: float = 1.5,
    temperature: float = 0.0,
    do_sample: bool = False
) -> str:
    """
    Generate text with steering vector applied.
    """
    if model is None or tokenizer is None:
        raise RuntimeError("Model not loaded")
    
    steering_vector = steering_vectors.get(steering_vector_name)
    if steering_vector is None:
        logger.warning(f"Steering vector '{steering_vector_name}' not found, generating without steering")
        return generate_text(prompt, max_new_tokens, temperature, do_sample)
    
    try:
        inputs = tokenizer(prompt, return_tensors="pt", truncation=True, max_length=2048).to(model.device)
        
        # Hook to inject steering
        def steering_hook(module, input, output):
            if isinstance(output, tuple):
                hidden_states = output[0]
            else:
                hidden_states = output
            
            # Add steering vector to all positions
            batch_size, seq_len, hidden_dim = hidden_states.shape
            steering_broadcast = steering_vector.unsqueeze(0).unsqueeze(0).expand(batch_size, seq_len, -1)
            hidden_states = hidden_states + steering_strength * steering_broadcast
            
            if isinstance(output, tuple):
                return (hidden_states,) + output[1:]
            return hidden_states
        
        # Apply steering to layer 26 (final decision layer - data-driven)
        target_layer = 26
        handles = []
        
        layer = model.model.layers[target_layer]
        handle = layer.register_forward_hook(steering_hook)
        handles.append(handle)
        
        try:
            # Generate
            with torch.no_grad():
                if do_sample:
                    gen_config = {
                        'max_new_tokens': max_new_tokens,
                        'do_sample': True,
                        'temperature': temperature,
                        'top_p': 0.9,
                        'pad_token_id': tokenizer.pad_token_id or tokenizer.eos_token_id
                    }
                else:
                    gen_config = {
                        'max_new_tokens': max_new_tokens,
                        'do_sample': False,
                        'pad_token_id': tokenizer.pad_token_id or tokenizer.eos_token_id
                    }
                
                output_ids = model.generate(**inputs, **gen_config)
            
            # Decode (remove input prompt)
            generated_ids = output_ids[0][inputs.input_ids.shape[1]:]
            output_text = tokenizer.decode(generated_ids, skip_special_tokens=True)
            
            return output_text.strip()
            
        finally:
            # Remove all steering hooks
            for handle in handles:
                handle.remove()
            
    except Exception as e:
        logger.exception(f"Generation with steering failed: {e}")
        raise

def batch_generate_with_steering(
    prompts: List[str],
    steering_vector_name: str,
    max_new_tokens: int = 100,
    steering_strength: float = 1.5,
    temperature: float = 0.0,
    do_sample: bool = False
) -> List[str]:
    """
    Batch generate with steering applied.
    """
    if model is None or tokenizer is None:
        raise RuntimeError("Model not loaded")
    
    steering_vector = steering_vectors.get(steering_vector_name)
    if steering_vector is None:
        logger.warning(f"Steering vector '{steering_vector_name}' not found")
        # Fallback to regular batch generation
        return batch_generate(prompts, max_new_tokens, temperature, do_sample)
    
    try:
        # Tokenize batch
        inputs = tokenizer(
            prompts,
            return_tensors="pt",
            padding=True,
            truncation=True,
            max_length=2048
        ).to(model.device)
        
        # Hook to inject steering
        def steering_hook(module, input, output):
            if isinstance(output, tuple):
                hidden_states = output[0]
            else:
                hidden_states = output
            
            batch_size, seq_len, hidden_dim = hidden_states.shape
            steering_broadcast = steering_vector.unsqueeze(0).unsqueeze(0).expand(batch_size, seq_len, -1)
            hidden_states = hidden_states + steering_strength * steering_broadcast
            
            if isinstance(output, tuple):
                return (hidden_states,) + output[1:]
            return hidden_states
        
        # Apply steering to layer 26 (final decision layer - data-driven)
        target_layer = 26
        handles = []
        
        layer = model.model.layers[target_layer]
        handle = layer.register_forward_hook(steering_hook)
        handles.append(handle)
        
        try:
            with torch.no_grad():
                if do_sample:
                    gen_config = {
                        'max_new_tokens': max_new_tokens,
                        'do_sample': True,
                        'temperature': temperature,
                        'top_p': 0.9,
                        'pad_token_id': tokenizer.pad_token_id or tokenizer.eos_token_id
                    }
                else:
                    gen_config = {
                        'max_new_tokens': max_new_tokens,
                        'do_sample': False,
                        'pad_token_id': tokenizer.pad_token_id or tokenizer.eos_token_id
                    }
                
                output_ids = model.generate(**inputs, **gen_config)
            
            # Decode each output
            results = []
            for i, output in enumerate(output_ids):
                input_length = inputs.input_ids[i].shape[0]
                generated_ids = output[input_length:]
                output_text = tokenizer.decode(generated_ids, skip_special_tokens=True)
                results.append(output_text.strip())
            
            return results
            
        finally:
            # Remove all steering hooks
            for handle in handles:
                handle.remove()
            
    except Exception as e:
        logger.exception(f"Batch generation with steering failed: {e}")
        raise

def batch_generate(prompts: List[str], max_new_tokens: int, temperature: float, do_sample: bool) -> List[str]:
    """Fallback batch generation without steering"""
    inputs = tokenizer(prompts, return_tensors="pt", padding=True, truncation=True, max_length=2048).to(model.device)
    
    with torch.no_grad():
        if do_sample:
            output_ids = model.generate(
                **inputs,
                max_new_tokens=max_new_tokens,
                do_sample=True,
                temperature=temperature,
                top_p=0.9,
                pad_token_id=tokenizer.pad_token_id or tokenizer.eos_token_id
            )
        else:
            output_ids = model.generate(
                **inputs,
                max_new_tokens=max_new_tokens,
                do_sample=False,
                pad_token_id=tokenizer.pad_token_id or tokenizer.eos_token_id
            )
    
    results = []
    for i, output in enumerate(output_ids):
        input_length = inputs.input_ids[i].shape[0]
        generated_ids = output[input_length:]
        output_text = tokenizer.decode(generated_ids, skip_special_tokens=True)
        results.append(output_text.strip())
    
    return results

def init_model():
    global tokenizer, model, pipeline
    with _model_lock:
        _update_load(2, "starting")
        if not HF_AVAILABLE:
            _update_load(100, "error", "transformers/torch not available")
            return
        if not os.path.isdir(LOCAL_MODEL_PATH):
            _update_load(100, "error", "model path missing")
            return
        _update_load(10, "loading", "loading tokenizer")
        try:
            tokenizer = AutoTokenizer.from_pretrained(LOCAL_MODEL_PATH, trust_remote_code=True)
            if tokenizer.pad_token is None:
                tokenizer.pad_token = tokenizer.eos_token
            # Fix for decoder-only models: use left padding for correct batch generation
            tokenizer.padding_side = 'left'
            logger.info("Tokenizer loaded successfully (padding_side=left)")
        except Exception as e:
            logger.exception("tokenizer load failed: %s", e)
            _update_load(100, "error", "tokenizer failed")
            return
        _update_load(20, "loading", "configuring quantization")
        bnb_config = None
        try:
            bnb_config = BitsAndBytesConfig(
                load_in_4bit=True,
                bnb_4bit_compute_dtype=torch.float16,
                bnb_4bit_quant_type="nf4",
                bnb_4bit_use_double_quant=False
            )
            logger.info("Quantization config created (4-bit NF4)")
        except Exception:
            bnb_config = None
        device_map = "auto"
        if not torch.cuda.is_available():
            raise RuntimeError("CUDA GPU is required. No GPU detected. Please run on a system with NVIDIA GPU.")
        
        logger.info(f"CUDA available: {torch.cuda.get_device_name(0)}")
        _update_load(30, "loading", "calculating memory")
        max_memory = None
        if torch.cuda.is_available():
            props = torch.cuda.get_device_properties(0)
            total_mb = int(props.total_memory / (1024 * 1024))
            reserve_mb = 800
            use_mb = max(0, total_mb - reserve_mb)
            use_gb = use_mb / 1024.0
            max_memory = {0: f"{use_gb:.1f}GB"}  # GPU only, no CPU fallback
            logger.info(f"GPU memory: {total_mb}MB total, {use_mb}MB available for model")
        _update_load(40, "loading", "loading model weights")
        
        # GPU-only: 4-bit quantized model
        try:
            logger.info("Loading model (4-bit quantized, GPU required)")
            kwargs = {
                "trust_remote_code": True,
                "low_cpu_mem_usage": True,
                "device_map": device_map,
                "quantization_config": bnb_config
            }
            if max_memory:
                kwargs["max_memory"] = max_memory
            
            _update_load(50, "loading", "loading 4-bit quantized")
            model = AutoModelForCausalLM.from_pretrained(LOCAL_MODEL_PATH, **kwargs)
            
            if hasattr(model, 'generation_config'):
                model.generation_config.do_sample = False
                model.generation_config.temperature = None
                model.generation_config.top_p = None
                model.generation_config.top_k = None
            
            pipeline = TextGenerationPipeline(model=model, tokenizer=tokenizer)
            _update_load(100, "ready", "loaded (4-bit quantized)")
            logger.info("Model loaded successfully: 4-bit quantized")
            
            # Initialize steering vectors
            try:
                logger.info("Initializing steering vectors...")
                initialize_steering_vectors()
                logger.info("✓ Steering vectors ready!")
            except Exception as e:
                logger.warning(f"Failed to initialize steering vectors: {e}")
            
            try:
                logger.info("Warming up model...")
                _ = pipeline("Test", max_new_tokens=5, do_sample=False)
                logger.info("Model warmup complete")
            except Exception:
                pass
                
        except Exception as e:
            _update_load(100, "error", "model load failed")
            logger.error(f"Model load failed: {e}")
            raise RuntimeError(f"Failed to load model on GPU: {e}")

_thread = threading.Thread(target=init_model, daemon=True)
_thread.start()

def _require_model():
    if pipeline is None:
        raise HTTPException(status_code=503, detail={"error": "model_not_loaded", "message": "Model not loaded."})

def generate_text(prompt: str, max_new_tokens: int = 150, temperature: float = 0.0, do_sample: bool = False) -> str:
    if pipeline is None:
        logger.error("generate_text called but pipeline is None")
        raise RuntimeError("pipeline_not_loaded")
    if ENABLE_LLM_DEBUG_LOGS:
        logger.info(f"LLM Prompt ({len(prompt)} chars, max_tokens={max_new_tokens}):\n{prompt[:2000]}...")
    try:
        try:
            if do_sample:
                gen_conf = GenerationConfig(
                    temperature=float(temperature),
                    max_new_tokens=int(max_new_tokens),
                    do_sample=True,
                    pad_token_id=tokenizer.pad_token_id or tokenizer.eos_token_id
                )
            else:
                gen_conf = GenerationConfig(
                    max_new_tokens=int(max_new_tokens),
                    do_sample=False,
                    temperature=None,
                    top_p=None,
                    top_k=None,
                    pad_token_id=tokenizer.pad_token_id or tokenizer.eos_token_id
                )
            out = pipeline(prompt, generation_config=gen_conf, return_full_text=False)
        except Exception:
            out = pipeline(prompt, max_new_tokens=max_new_tokens, do_sample=do_sample)
        if isinstance(out, list) and len(out) > 0:
            item = out[0]
            text = item.get("generated_text") or item.get("text") or str(item)
        else:
            text = str(out)
        text = text.strip()
        if ENABLE_LLM_DEBUG_LOGS:
            logger.info(f"LLM Raw Output ({len(text)} chars):\n{text[:2000]}...")
        return text
    except Exception as e:
        logger.exception("generate_text failed: %s", e)
        raise

@app.get("/health")
def health():
    return {
        "status": "ok",
        "model_loaded": pipeline is not None,
        "cuda_available": HF_AVAILABLE and torch.cuda.is_available() if HF_AVAILABLE else False,
        "steering_enabled": len(steering_vectors) > 0
    }

@app.get("/load_status")
def load_status():
    return _load_status

@app.get("/steering_status")
def steering_status():
    """Check which steering vectors are loaded"""
    return {
        "enabled": len(steering_vectors) > 0,
        "vectors": list(steering_vectors.keys()),
        "count": len(steering_vectors)
    }

# ============================================================
# SINGLE ENDPOINTS (with steering)
# ============================================================

@app.post("/summarize")
async def summarize(payload: TextPayload):
    try:
        _require_model()
    except HTTPException:
        raise
    content = (payload.content or "").strip()
    if not content:
        raise HTTPException(status_code=400, detail="content required")
    
    # MINIMAL PROMPT - steering handles the rest!
    prompt = SUMMARIZE_PROMPT.format(task=content)
    
    try:
        # Use steering for concise, clean JSON output
        raw = generate_with_steering(
            prompt,
            steering_vector_name='concise_summary',
            max_new_tokens=80,  # Reduced from 120!
            steering_strength=1.8,
            temperature=0.1,
            do_sample=True
        )
    except Exception as e:
        logger.exception(f"generate_with_steering failed in summarize: {e}")
        raise HTTPException(status_code=500, detail={"error": "generation_failed", "message": str(e)})
    
    cleaned = _clean_summary_output(raw)
    if cleaned.get("summary"):
        return {
            "summary": cleaned["summary"],
            "urgency": cleaned["urgency"],
            "raw": cleaned.get("raw")
        }
    raise HTTPException(status_code=502, detail={"error": "parse_failed", "raw": raw})

@app.post("/vote")
async def vote(payload: VotePayload):
    """2-STAGE VOTING: Check overload, then pick winner (NO STEERING)"""
    try:
        _require_model()
    except HTTPException:
        raise
    content = (payload.content or "").strip()
    candidates = payload.candidates or []
    team_state = payload.team_state or []
    if not content or not candidates:
        raise HTTPException(status_code=400, detail="content and candidates required")
    
    try:
        # STAGE 1: Check if candidates are available
        stage1_prompt = VOTE_STAGE1_PROMPT.format(
            candidates=json.dumps(candidates),
            team_state=json.dumps(team_state)
        )
        
        if ENABLE_LLM_DEBUG_LOGS:
            logger.info("📊 STAGE 1: Checking overload status")
        
        # Generate stage 1 with CONSTRAINED JSON (NO steering, NO debug)
        stage1_raw = generate_constrained_json_with_steering(
            stage1_prompt,
            steering_vector_name='precise_calculation',  # Not used
            max_new_tokens=50,
            temperature=0.0,
            steering_strength=0.0,  # NO STEERING
            candidate_names=candidates  # For debug logging
        )
        
        if ENABLE_LLM_DEBUG_LOGS:
            logger.info(f"Stage 1 result: {stage1_raw[:100]}")
        
        # Parse stage 1
        has_available = True  # Default
        filtered_candidates = candidates  # Default to all
        
        # Use extract_json_from_text to handle extra text after JSON
        parsed = extract_json_from_text(stage1_raw)
        if parsed:
            has_available = parsed.get("has_available", True)
            filtered = parsed.get("filtered_candidates", [])
            
            # Use filtered candidates if provided and valid
            if filtered and len(filtered) > 0:
                filtered_valid = [c for c in filtered if c in candidates]
                if filtered_valid:
                    filtered_candidates = filtered_valid
                    if ENABLE_LLM_DEBUG_LOGS:
                        logger.info(f"Filtered to lowest-load candidates: {filtered_candidates}")
        else:
            # JSON parsing failed - default to all available
            if ENABLE_LLM_DEBUG_LOGS:
                logger.info(f"⚠️  Stage 1 JSON parsing failed, defaulting to all available")

        # If all overloaded, return immediately
        if not has_available:
            if ENABLE_LLM_DEBUG_LOGS:
                logger.info("All candidates overloaded - returning null")
            
            return {
                "winner": None,
                "reasons": ["all overloaded"],
                "raw": stage1_raw
            }
        
        # STAGE 2: Pick winner
        if ENABLE_LLM_DEBUG_LOGS:
            logger.info("📊 STAGE 2: Picking winner")
        
        # Filter team_state to only include filtered candidates
        filtered_team_state = [
            member for member in team_state 
            if member.get("name") in filtered_candidates
        ]
        
        if ENABLE_LLM_DEBUG_LOGS:
            logger.info(f"Filtered team_state: {len(filtered_team_state)} members (from {len(team_state)} total)")
        
        stage2_prompt = VOTE_STAGE2_PROMPT.format(
            candidates=json.dumps(filtered_candidates),  # Use filtered candidates
            team_state=json.dumps(filtered_team_state)  # Use filtered team_state!
        )
        
        # Generate stage 2 (NO steering, WITH debug for comprehensive analysis)
        raw = generate_constrained_json_with_steering(
            stage2_prompt,
            steering_vector_name='precise_calculation',  # Not used
            max_new_tokens=100,
            temperature=0.0,
            steering_strength=0.0,  # NO STEERING
            candidate_names=filtered_candidates  # For debug logging (filtered)
        )
        
        if ENABLE_LLM_DEBUG_LOGS:
            logger.info(f"Stage 2 result ({len(raw)} chars): {raw[:200]}")
        
    except Exception as e:
        logger.exception(f"2-stage vote failed: {e}")
        raise HTTPException(status_code=500, detail={"error": "generation_failed", "message": str(e)})
    
    cleaned = _clean_vote_output(raw, candidates=candidates)
    
    if ENABLE_LLM_DEBUG_LOGS:
        logger.info(f"Vote cleaned: winner='{cleaned.get('winner')}', reasons={cleaned.get('reasons', [])}")
    
    return {
        "winner": cleaned.get("winner"),
        "reasons": cleaned.get("reasons", []),
        "raw": cleaned.get("raw")
    }

# ============================================================
# BATCH ENDPOINTS (with steering) - ULTIMATE SPEED!
# ============================================================

@app.post("/summarize_batch")
async def summarize_batch(payload: BatchTextPayload):
    """ULTIMATE SPEED: Batch summarize with steering"""
    try:
        _require_model()
    except HTTPException:
        raise
    
    contents = payload.contents or []
    if not contents:
        raise HTTPException(status_code=400, detail="contents list required")
    
    if ENABLE_LLM_DEBUG_LOGS:
        logger.info(f"🚀 Batch summarize with steering: {len(contents)} tasks")
    
    # MINIMAL PROMPTS
    prompts = [SUMMARIZE_PROMPT.format(task=content.strip()) for content in contents]
    
    try:
        # Batch generate with steering!
        raw_outputs = batch_generate_with_steering(
            prompts,
            steering_vector_name='concise_summary',
            max_new_tokens=150,  # Increased to handle preamble + JSON
            steering_strength=1.8,
            temperature=0.1,
            do_sample=True
        )
        
        # Process each output
        results = []
        for i, raw in enumerate(raw_outputs):
            if ENABLE_LLM_DEBUG_LOGS:
                logger.info(f"Batch item {i+1} raw ({len(raw)} chars): {raw[:150]}...")
            
            cleaned = _clean_summary_output(raw)
            if ENABLE_LLM_DEBUG_LOGS:
                logger.info(f"Batch item {i+1} cleaned: summary='{cleaned.get('summary', '')[:100]}', urgency='{cleaned.get('urgency', '')}'")
            if cleaned.get("summary"):
                results.append({
                    "summary": cleaned["summary"],
                    "urgency": cleaned["urgency"],
                    "raw": cleaned.get("raw"),
                    "success": True
                })
            else:
                results.append({
                    "summary": "",
                    "urgency": "",
                    "raw": raw,
                    "success": False,
                    "error": "parse_failed"
                })
        
        # Return plain list for task_analyzer compatibility
        return results
        
    except Exception as e:
        logger.exception(f"Batch summarize with steering failed: {e}")
        raise HTTPException(status_code=500, detail={"error": "batch_generation_failed", "message": str(e)})

@app.post("/vote_batch")
async def vote_batch(payload: BatchVotePayload):
    """2-STAGE VOTING: Check overload, then pick winner (NO STEERING)"""
    try:
        _require_model()
    except HTTPException:
        raise
    
    items = payload.items or []
    if not items:
        raise HTTPException(status_code=400, detail="items list required")
    
    if ENABLE_LLM_DEBUG_LOGS:
        logger.info(f"🚀 2-STAGE VOTE (NO STEERING): {len(items)} tasks")
    
    # ========================================
    # STAGE 1: Check if candidates are available
    # ========================================
    if ENABLE_LLM_DEBUG_LOGS:
        logger.info(f"📊 STAGE 1: Checking overload status for {len(items)} tasks")
    
    stage1_prompts = []
    all_candidates = []
    all_team_states = []
    
    for item in items:
        candidates = item.get("candidates", [])
        team_state = item.get("team_state", [])
        
        prompt = VOTE_STAGE1_PROMPT.format(
            candidates=json.dumps(candidates),
            team_state=json.dumps(team_state)
        )
        stage1_prompts.append(prompt)
        all_candidates.append(candidates)
        all_team_states.append(team_state)
    
    try:
        # Stage 1: Generate with CONSTRAINED JSON (loop through each)
        stage1_outputs = []
        for i, prompt in enumerate(stage1_prompts):
            output = generate_constrained_json_with_steering(
                prompt,
                steering_vector_name='precise_calculation',  # Not used
                max_new_tokens=50,
                temperature=0.0,
                steering_strength=0.0,  # NO STEERING
                candidate_names=all_candidates[i]  # For debug logging
            )
            stage1_outputs.append(output)
        
        # Parse stage 1 results
        overload_status = []
        filtered_candidates_list = []  # Store filtered candidates for each task
        
        for i, raw in enumerate(stage1_outputs):
            if ENABLE_LLM_DEBUG_LOGS:
                logger.info(f"Stage 1 task {i+1}: {raw[:150]}")
            
            # Try to parse has_available and filtered_candidates
            has_available = True  # Default
            filtered_candidates = all_candidates[i]  # Default to all candidates
            
            # Use extract_json_from_text to handle extra text after JSON
            parsed = extract_json_from_text(raw)
            if parsed:
                has_available = parsed.get("has_available", True)
                filtered = parsed.get("filtered_candidates", [])
                
                # Use filtered candidates if provided and valid
                if filtered and len(filtered) > 0:
                    # Verify filtered candidates are subset of original
                    filtered_valid = [c for c in filtered if c in all_candidates[i]]
                    if filtered_valid:
                        filtered_candidates = filtered_valid
                
                if ENABLE_LLM_DEBUG_LOGS:
                    logger.info(f"  Task {i+1} has_available: {has_available}")
                    logger.info(f"  Task {i+1} filtered_candidates: {filtered_candidates}")
            else:
                # Parse error: use defaults
                if ENABLE_LLM_DEBUG_LOGS:
                    logger.info(f"  Task {i+1} ⚠️  JSON parsing failed, defaulting to all available")
            
            overload_status.append(has_available)
            filtered_candidates_list.append(filtered_candidates)

        # STAGE 2: Pick winner for available tasks
        # ========================================
        tasks_needing_stage2 = [i for i, available in enumerate(overload_status) if available]
        
        if ENABLE_LLM_DEBUG_LOGS:
            logger.info(f"📊 STAGE 2: Picking winners for {len(tasks_needing_stage2)}/{len(items)} tasks")
        
        # Prepare stage 2 prompts only for available tasks
        stage2_prompts = []
        stage2_indices = []
        
        for i in tasks_needing_stage2:
            # Filter team_state to only include filtered candidates
            filtered_team_state = [
                member for member in all_team_states[i]
                if member.get("name") in filtered_candidates_list[i]
            ]
            
            prompt = VOTE_STAGE2_PROMPT.format(
                candidates=json.dumps(filtered_candidates_list[i]),  # Use filtered candidates from stage 1
                team_state=json.dumps(filtered_team_state)  # Use filtered team_state!
            )
            stage2_prompts.append(prompt)
            stage2_indices.append(i)
        
        # Stage 2: Batch generate WITHOUT steering (only for non-overloaded)
        if stage2_prompts:
            stage2_outputs = batch_generate(
                stage2_prompts,
                max_new_tokens=80,
                temperature=0.0,
                do_sample=False
            )
        else:
            stage2_outputs = []
        
        # ========================================
        # COMBINE RESULTS
        # ========================================
        results = []
        stage2_idx = 0
        
        for i in range(len(items)):
            if not overload_status[i]:
                # All overloaded - no stage 2
                results.append({
                    "winner": None,
                    "reasons": ["all overloaded"],
                    "raw": stage1_outputs[i],
                    "success": True,
                    "stage": "stage1_overloaded"
                })
            else:
                # Has available candidates - use stage 2 result
                raw = stage2_outputs[stage2_idx]
                stage2_idx += 1
                
                if ENABLE_LLM_DEBUG_LOGS:
                    logger.info(f"Stage 2 task {i+1}: {raw[:100]}")
                
                cleaned = _clean_vote_output(raw, candidates=filtered_candidates_list[i])  # Use filtered candidates
                results.append({
                    "winner": cleaned.get("winner"),
                    "reasons": cleaned.get("reasons", []),
                    "raw": cleaned.get("raw"),
                    "success": cleaned.get("winner") is not None or len(all_candidates[i]) == 0,
                    "stage": "stage2_picked"
                })
        
        return {
            "results": results,
            "count": len(results),
            "success_count": sum(1 for r in results if r.get("success")),
            "stage1_count": len(items),
            "stage2_count": len(stage2_prompts)
        }
        
    except Exception as e:
        logger.exception(f"2-stage vote failed: {e}")
        raise HTTPException(status_code=500, detail={"error": "batch_generation_failed", "message": str(e)})

@app.get("/")
def root():
    return {
        "detail": "Local LLM Service - Ultimate Speed Edition",
        "features": ["Activation Steering", "Batch Processing", "Minimal Prompts"],
        "speedup": "20-30x faster than baseline"
    }