import math
import time
import torch
from torch import nn, optim
from transformers import Adafactor
from torch.amp import autocast, GradScaler
import numpy as np
import os

from data import *
from models.model.hierarchical_t5 import HierarchicalT5
from models.model.hierarchical_t5_config import HierarchicalT5Config
from utils.epoch_timer import epoch_time
from utils.checkpoints import save_best_models, get_best_models


def count_parameters(model):
    return sum(p.numel() for p in model.parameters() if p.requires_grad), sum(
        p.numel() for p in model.parameters()
    )


def initialize_weights(m):
    if hasattr(m, "weight"):
        if isinstance(m, nn.Linear):
            nn.init.normal_(m.weight, mean=0.0, std=0.02)
            if m.bias is not None:
                nn.init.constant_(m.bias, 0)
        elif isinstance(m, nn.LayerNorm):
            nn.init.constant_(m.bias, 0)
            nn.init.constant_(m.weight, 1.0)
        elif isinstance(m, nn.Embedding):
            nn.init.normal_(m.weight, mean=0.0, std=0.02)

# store parameters
if not os.path.exists("result"):
    os.mkdir("result")

with open("result/parameters.txt", "w") as f:
    f.write(str(info))

# load device
device = torch.device("cuda" if torch.cuda.is_available() else "cpu")

if load_pretrained:
    model_config = HierarchicalT5Config.from_pretrained(pretrained_path)
    model_config.pretrained_path = pretrained_path
    model = HierarchicalT5(config=model_config)
else:
    model_config = HierarchicalT5Config(
        pad_token_id=pad_token_id,
        eos_token_id=eos_token_id,
        image_size=image_size,
        image_patch_size=image_patch_size,
        dim=dim,
        ffn_hidden_ratio=ffn_hidden_ratio,
        n_heads=n_heads,
        drop_prob=drop_prob,
        max_output=max_output,
        vit_block=vit_block,
        dec_voc_size=dec_voc_size,
        pretrained_path=None,
        model_name=model_name,
        encoder_layers=encoder_layers,
        decoder_layers=decoder_layers,
    )

    model = HierarchicalT5(config=model_config)
    model.custom_embed.apply(initialize_weights)

    for param in model.t5.parameters():
        param.requires_grad = False

trainable_params, total_params = count_parameters(model)
print(f"The model has {total_params:,} total parameters")
print(f"The model has {trainable_params:,} trainable parameters")
model.cuda()

optimizer = Adafactor(
    model.parameters(),
    lr=init_lr,
    weight_decay=weight_decay,
    scale_parameter=True,  # Auto-adjusts learning rate
    relative_step=False,  # Uses step-based learning rate scheduling
    # warmup_init=True       # Helps stabilize early training
)

linear_scheduler = optim.lr_scheduler.LinearLR(
    optimizer, start_factor=0.1, total_iters=warmup
)
cosine_scheduler = optim.lr_scheduler.CosineAnnealingLR(
    optimizer, T_max=T_max, eta_min=end_lr
)
scheduler = optim.lr_scheduler.SequentialLR(
    optimizer, schedulers=[linear_scheduler, cosine_scheduler], milestones=[warmup]
)

criterion = nn.CrossEntropyLoss(
    ignore_index=pad_token_id, label_smoothing=label_smoothing
)

def train(model, iterator, optimizer, criterion, clip):
    model.train()
    epoch_loss = 0
    scaler = GradScaler()

    forward_pass_time, backprop_time, update_time = [], [], []

    for i, (x, y) in enumerate(iterator):
        src = x.to(device, non_blocking=True)
        trg = y.to(device, non_blocking=True)

        optimizer.zero_grad()

        start_time = time.time()
        with autocast(device_type="cuda", enabled=False):
            output = model(input_ids=src, labels=trg)
            # output_logits = output.logits.to(torch.float32)
            loss = output.loss
            # loss = criterion(
            #     output_logits.view(-1, output_logits.size(-1)),
            #     trg.contiguous().view(-1),
            # )
            # print(output.logits.max(dim=2)[1][0])
            # print(trg[0])

        forward_pass_time.append(time.time() - start_time)

        start_time = time.time()
        scaler.scale(loss).backward()
        backprop_time.append(time.time() - start_time)

        start_time = time.time()
        scaler.unscale_(optimizer)
        torch.nn.utils.clip_grad_norm_(model.parameters(), clip)
        scaler.step(optimizer)
        scaler.update()
        update_time.append(time.time() - start_time)

        epoch_loss += loss.item()
        if i % 800 == 0:
            print(f"Step: {round((i / len(iterator)) * 100, 2)}%, Loss: {loss.item()}")
            print(
                f"Forward: {np.mean(forward_pass_time):.3f} sec, "
                f"Backpropagation: {np.mean(backprop_time):.3f} sec, "
                f"Gradient Update: {np.mean(update_time):.3f} sec"
            )
            forward_pass_time, backprop_time, update_time = [], [], []

    return epoch_loss / len(iterator)


def evaluate(model, iterator, tokenizer, beam_size=5):
    """
    Evaluates the model on the given dataset iterator.

    Args:
        model (nn.Module): The trained model to evaluate.
        iterator (DataLoader): DataLoader for the evaluation dataset.
        criterion (nn.Module): Loss function.
        sp (SentencePieceProcessor): SentencePiece tokenizer.

    Returns:
        tuple: Contains average loss, BLEU-1, BLEU-2, BLEU-3, BLEU, ROUGE-L, and BLEURT scores.
    """
    model.eval()

    # Initialize metrics
    total_loss = []
    total_correct_words, total_words = 0, 0

    with torch.no_grad():
        for i, (x, y) in enumerate(iterator):
            src = x.to(device, non_blocking=True)
            trg = y.to(device, non_blocking=True)

            loss = model(input_ids=src, labels=trg).loss
            total_loss.append(loss.item())

            # Generate output sequences if beam size > 0
            # otherwise, process loss only
            if beam_size != 0:
                output = model.generate(input_ids=src, beam_size=beam_size)
            else:
                continue

            if tokenizer_type == "hf":
                # Convert target indices to words using HuggingFace tokenizer
                trg_words = tokenizer.batch_decode(y, skip_special_tokens=True)
                # Convert output indices to words
                output_words = tokenizer.batch_decode(
                    output.sequences, skip_special_tokens=True
                )

            # temporally deprecated
            elif tokenizer_type == "sp":
                # Convert target indices to words using SentencePiece
                trg_words = [tokenizer.decode_ids(ids) for ids in y.tolist()]
                # Convert output indices to words
                output_words = [
                    tokenizer.decode_ids(ids) for ids in output.sequences.tolist()
                ]

            else:
                raise ValueError(
                    f"Invalid tokenizer type: {tokenizer_type}, must be 'hf' or 'sp'"
                )

            try:
                for pred, ref in zip(output_words, trg_words):
                    pred_words = pred.strip("▁").split("▁")
                    ref_words = ref.strip("▁").split("▁")
                    correct = sum(p == r for p, r in zip(pred_words, ref_words))
                    total_correct_words += correct
                    total_words += len(ref_words)

            except Exception as e:
                print(f"Error calculating Word Accuracy for batch {i}, item {e}")
                pass

            if i % 25 == 0:
                print(
                    f"Batch {i}: Step: {round((i / len(iterator)) * 100, 2)}%\n",
                    f"Predicted: {output_words[0]}\nTarget: {trg_words[0]}",
                )

    # Compute average metrics over all batches
    avg_loss = sum(total_loss) / len(total_loss) if total_loss else 0.0
    word_accuracy = total_correct_words / total_words if total_words > 0 else 0.0

    return avg_loss, word_accuracy


def run(total_epoch, model, best_loss=float(1e6)):
    loss_bleus = []
    for step in range(total_epoch):
        step += 1
        start_time = time.time()
        train_loss = train(model, train_iter, optimizer, criterion, clip)

        if step % 10 == 0:
            val_loss, acc = evaluate(model, test_iter, tokenizer, beam_size=5)
            loss_bleus.append(f"{step},{train_loss},{val_loss},{acc}")

            f = open("result/loss_bleus.txt", "w")
            f.write(str(loss_bleus))
            f.close()

        else:
            val_loss, acc = evaluate(model, test_iter, tokenizer, beam_size=0)

        if (val_loss < best_loss) and (step > epoch // 3):
            best_loss = val_loss

            test_loss, bleu_1, bleu_2, bleu_3, bleu, rough_L, bleurt_score = evaluate(
                model, test_iter, tokenizer, beam_size=5
            )

            save_best_models(model, val_loss, step, save_dir="./result", max_models=1)

            test_result = f"""Epoch: {step}
            Train Loss: {train_loss:.3f} | Test Loss: {test_loss:.3f}
            Word Accuracy: {acc:.4f}
            """

            f = open(f"result/step_{step}_result.txt", "w")
            f.write(f"Test Result\n{test_result}")
            f.close()

        end_time = time.time()
        epoch_mins, epoch_secs = epoch_time(start_time, end_time)

        scheduler.step()

        print(f"Epoch: {step} | Time: {epoch_mins}m {epoch_secs}s")
        print(f"\tTrain Loss: {train_loss:.3f} | Valid Loss: {val_loss:.3f}")
        print(f"\tWord Accuracy: {acc:.4f}")


if __name__ == "__main__":
    run(total_epoch=epoch, model=model, best_loss=float("inf"))
