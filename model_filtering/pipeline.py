#!/usr/bin/env python3

# Standard library imports
import glob
import json
import os
import time
from datetime import timedelta

# Third party imports
import numpy as np
from datasets import Dataset
from rich.panel import Panel
from torch.utils.data import DataLoader
from tqdm import tqdm
from transformers import AutoTokenizer
from vllm import LLM, SamplingParams

# Local imports
from model_filtering.utils import console, json_default, custom_collate_fn

class DifficultyFilterPipeline:
    def __init__(self, args):
        self.args = args
        self.tokenizer = None
        self.model = None
        self.sampling_params = None

        self.start_time = time.time()
        self.gen_times = []

    @staticmethod
    def format_time(seconds):
        return str(timedelta(seconds=int(seconds)))

    # ------------- component init ------------------------------------------ #
    def initialize_components(self):
        console.print(f"🔄 Loading tokenizer from [highlight]{self.args.model_path}[/highlight]...")
        self.tokenizer = AutoTokenizer.from_pretrained(self.args.model_path)

        console.print(
            f"🚀 Initializing model from [highlight]{self.args.model_path}[/highlight] "
            f"with TP={self.args.tp_size}..."
        )
        console.print(f"Warning: enable_expert_parallel is set to {self.args.enable_expert_parallel}")
        console.print("If you are using MOE models, set enable_expert_parallel to True")
        
        self.model = LLM(
            self.args.model_path,
            tensor_parallel_size=self.args.tp_size,
            enable_expert_parallel=self.args.enable_expert_parallel,
            enforce_eager=True,
        )

        self.sampling_params = SamplingParams(
            n=self.args.n,
            max_tokens=self.args.max_new_tokens,
            top_k=self.args.top_k,
            top_p=self.args.top_p,
            temperature=self.args.temperature,
            repetition_penalty=self.args.repetition_penalty,
            skip_special_tokens=True,
        )
        console.print("✅ Model initialization [success]complete[/success].")

    # ------------- dataset -------------------------------------------------- #
    def prepare_dataset(self):
        console.print(f"📂 Loading dataset from [highlight]{self.args.dataset_parquet_path}[/highlight]...")
        if "livecodebench" in self.args.dataset_parquet_path:
            # livecodebench is too long so we need a different way to load it
            import polars as pl
            console.print(f"🐞 [DEBUG] Loading livecodebench dataset using polars")
            pd_data = pl.read_parquet(self.args.dataset_parquet_path).to_pandas()
            dataset = Dataset.from_pandas(pd_data)
        else:
            dataset = Dataset.from_parquet(self.args.dataset_parquet_path)
        
        console.print(f"🐞 [DEBUG] Dataset loaded with [highlight]{len(dataset)}[/highlight] samples")

        # TODO: replace None values with empty strings in dataset columns and nested dictionaries
        # TODO: Below sometimes causes stucks in the process
        # # Replace None values with empty strings in dataset columns and nested dictionaries
        # def replace_none_with_empty(obj, max_depth=3):
        #     if obj is None:
        #         return ""
        #     elif isinstance(obj, dict) and max_depth > 0:
        #         return {k: replace_none_with_empty(v, max_depth-1) for k, v in obj.items()}
        #     elif isinstance(obj, list) and max_depth > 0:
        #         return [replace_none_with_empty(item, max_depth-1) for item in obj]
        #     return obj

        # def process_example(example):
        #     return {k: replace_none_with_empty(v) for k, v in example.items()}

        # # Use batched processing with multiple workers
        # dataset = dataset.map(
        #     process_example,
        #     num_proc=min(8, os.cpu_count()),
        #     desc="Processing None values"
        # )
        # console.print("🧹 Replaced None values with empty strings using parallel processing")
            
        # ── debug slice
        if self.args.debug:
            dataset = dataset.select(range(min(48, len(dataset))))
            console.print(f"🐞 [DEBUG] Using first [highlight]{len(dataset)}[/highlight] samples")

        # ── DP split
        if self.args.dp_size > 1:
            total = len(dataset)
            per_rank = total // self.args.dp_size
            start = self.args.dp_rank * per_rank
            end = start + per_rank if self.args.dp_rank != self.args.dp_size - 1 else total
            dataset = dataset.select(range(start, end))
            console.print(
                f"🔢 DP rank [highlight]{self.args.dp_rank}[/highlight] "
                f"processing [highlight]{len(dataset)}[/highlight] / {total}"
            )
        else:
            console.print(f"📊 Dataset loaded with [highlight]{len(dataset)}[/highlight] samples")

        # ── KEEP-ONLY columns actually referenced downstream ---------------- #
        required_cols = {
            "prompt",
            "reward_model",
            "apply_chat_template",
            "data_source",
            "extra_info",
        }
        cols_to_drop = [c for c in dataset.column_names if c not in required_cols]
        if cols_to_drop:
            dataset = dataset.remove_columns(cols_to_drop)
            console.print(f"🧹 Dropped {len(cols_to_drop)} column(s) for easier processing: {', '.join(cols_to_drop)}")

        return dataset

    # ------------- checkpoint paths / I-O ------------------------------ #
    def get_checkpoint_path(self):
        model_name = self.args.model_path.split("/")[-1]
        dataset_name = os.path.basename(self.args.dataset_parquet_path).rsplit(".parquet", 1)[0]
        rank_output_dir = os.path.join(self.args.output_dir, dataset_name, model_name, f"dp{self.args.dp_rank}")
        os.makedirs(rank_output_dir, exist_ok=True)
        return os.path.join(rank_output_dir, "checkpoint.pkl")

    def load_checkpoint(self):
        model_name = self.args.model_path.split("/")[-1]
        dataset_name = os.path.basename(self.args.dataset_parquet_path).rsplit(".parquet", 1)[0]
        rank_output_dir = os.path.join(self.args.output_dir, dataset_name, model_name, f"dp{self.args.dp_rank}")
        os.makedirs(rank_output_dir, exist_ok=True)
        
        # If force_regenerate is set, start from scratch
        if self.args.force_regenerate:
            console.print("🔄 [warning]Force regeneration requested - ignoring existing checkpoints[/warning]")
            return {"current_batch_idx": 0, "global_results": {}, "global_errors": {}}
        
        # Find batch files and extract their numbers
        batch_files = glob.glob(os.path.join(rank_output_dir, "batch_*.json"))
        batch_files_with_idx = []
        
        for batch_file in batch_files:
            try:
                batch_num = int(os.path.basename(batch_file).split("_")[1].split(".")[0])
                batch_files_with_idx.append((batch_num, batch_file))
            except (ValueError, IndexError):
                console.print(f"⚠️ [warning]Could not parse batch number from {batch_file}[/warning]")
        
        # Sort by batch number
        batch_files_with_idx.sort(key=lambda x: x[0])
        
        # Load results from existing batch files
        global_results = {}
        global_errors = {}
        
        if batch_files_with_idx:
            console.print(f"📋 Found {len(batch_files_with_idx)} existing batch files to load")
            
            for batch_idx, batch_file in batch_files_with_idx:
                try:
                    with open(batch_file, 'r') as f:
                        batch_data = json.load(f)
                    # Directly load results (no reward info expected here)
                    for i, result in batch_data.items():
                        global_results[f"{batch_idx}_{i}"] = result
                    console.print(f"✅ Loaded batch {batch_idx} with {len(batch_data)} results")
                except Exception as e:
                    console.print(f"⚠️ [warning]Failed to load batch file {batch_file}: {e}[/warning]")
        
        # Determine the next batch index to start from
        start_batch_idx = batch_files_with_idx[-1][0] + 1 if batch_files_with_idx else 0
        
        if batch_files_with_idx:
            console.print(f"📋 Resuming from batch {start_batch_idx} with {len(global_results)} loaded results")
        
        return {"current_batch_idx": start_batch_idx, "global_results": global_results, "global_errors": global_errors}

    # ------------- batch serialization  ------------------------------------ #
    def process_batch_outputs(self, outputs, batch_dict):
        batch_results = {}

        for i in range(len(outputs)):
            # full model outputs
            full_responses = [r.text for r in outputs[i].outputs]

            data_source   = batch_dict["data_source"][i]
            ground_truth  = batch_dict["reward_model"]["ground_truth"][i]
            messages      = batch_dict["prompt"][i]
            question      = next((m["content"] for m in messages if m["role"] == "user"), "No question found")
            extra_info    = {k: batch_dict["extra_info"][k][i] for k in batch_dict["extra_info"]}

            batch_results[i] = {
                "messages":    messages,
                "question":    question,
                "ground_truth": ground_truth,
                "source":      data_source,
                "responses":   full_responses,
                "extra_info":  extra_info,
            }

        return batch_results

    # ------------- progress ------------------------------------------------- #
    def print_progress_stats(self, idx, total_batches):
        elapsed = time.time() - self.start_time
        eta = "calculating..."
        if idx > 0 and self.gen_times:
            remain = total_batches - idx - 1
            eta_batch = np.mean(self.gen_times[-10:])
            eta = self.format_time(remain * eta_batch)

        console.print()
        console.print(
            Panel(
                f"[bold]Progress: [metric]{idx+1}/{total_batches}[/metric] "
                f"({(idx+1)/total_batches*100:.1f}%)[/bold]",
                title="📊 Summary",
                border_style="cyan",
            )
        )
        console.print(f"⏱️  Elapsed: [time]{self.format_time(elapsed)}[/time] | ETA: [time]{eta}[/time]")
        if self.gen_times:
            console.print(f"⚡ Generation avg (last 10): [metric]{np.mean(self.gen_times[-10:]):.2f}s[/metric]")

    # ------------- main loop ------------------------------------------------ #
    def run_inference(self):
        self.initialize_components()
        dataset = self.prepare_dataset()

        dataloader = DataLoader(
            dataset,
            batch_size=self.args.batch_size,
            pin_memory=True,
            num_workers=min(4, os.cpu_count() // max(1, self.args.dp_size)),
            prefetch_factor=8,
            collate_fn=custom_collate_fn
        )

        chk = self.load_checkpoint()
        start_batch_idx = chk["current_batch_idx"]
        global_results  = chk["global_results"]
        global_errors   = chk["global_errors"]

        model_name   = self.args.model_path.split("/")[-1]
        dataset_name = os.path.basename(self.args.dataset_parquet_path).rsplit(".parquet", 1)[0]
        rank_output_dir = os.path.join(
            self.args.output_dir, dataset_name, model_name, f"dp{self.args.dp_rank}"
        )
        os.makedirs(rank_output_dir, exist_ok=True)

        progress_bar = tqdm(
            total=len(dataloader),
            initial=start_batch_idx,
            desc=f"🔄 DP{self.args.dp_rank} Processing",
            position=0,
        )

        batch_iter = iter(dataloader)
        for _ in range(start_batch_idx):
            try:
                next(batch_iter)
            except StopIteration:
                console.print(f"⚠️ [warning]Checkpoint index {start_batch_idx} exceeds dataset size[/warning]")
                break

        for idx, batch_dict in enumerate(batch_iter, start=start_batch_idx):
            batch_size = len(batch_dict["reward_model"]["ground_truth"])
            console.print(f"\n🔄 Generating for batch {idx}/{len(dataloader)-1} ({batch_size} samples)…")            
            
            # enforce apply_chat_template==True
            if not all(batch_dict.get("apply_chat_template", [True] * batch_size)):
                raise RuntimeError(
                    "Encountered apply_chat_template=False but raw_prompt column is removed. "
                    "Please ensure all samples set apply_chat_template=True."
                )

            gen_start = time.time()
            outputs = self.model.chat(
                    batch_dict["prompt"],
                    sampling_params=self.sampling_params
            )
            
            self.gen_times.append(time.time() - gen_start)
            console.print(f"⏱️  Generation took [time]{self.gen_times[-1]:.2f}s[/time]")

            # ----------- store outputs ------------------------- #
            batch_results = self.process_batch_outputs(outputs, batch_dict)
            global_results.update({f"{idx}_{i}": r for i, r in batch_results.items()})

            batch_out = os.path.join(rank_output_dir, f"batch_{idx:05d}.json")
            with open(batch_out, "w") as f:
                json.dump(batch_results, f, indent=2, default=json_default)
            console.print(f"💾 Saved batch results to [highlight]{batch_out}[/highlight]")

            self.print_progress_stats(idx, len(dataloader))
            progress_bar.update(1)
            console.rule(style="cyan")

        progress_bar.close()
        elapsed_total = time.time() - self.start_time

        console.print()
        console.print(Panel("[bold]Inference completed!", title="🏁 Finished", border_style="green"))
        console.print(f"⏱️  Total time: [time]{self.format_time(elapsed_total)}[/time]")
        console.print(
            "ℹ️ All per-batch JSON files are ready. "
            "Run the separate reward script to score and assemble final results."
        )

        # temporary sleep to avoid closing the process
        time.sleep(10000)