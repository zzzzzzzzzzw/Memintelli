# -*- coding:utf-8 -*-
# @File  : 05_ddp_mnist_training.py
# @Author: ZZW
# @Date  : 2025/2/9
"""Memintelli example 5: Multiple layer inference with MLP on MNIST.
This example demonstrates the usage of Memintelli with a simple MLP classifier that has been trained in software.

python -m torch.distributed.run --nproc_per_node=2 examples/05_ddp_mnist_training.py 
"""

import os
import sys
import torch
from torch import nn, optim
from torchvision import datasets, transforms
from tqdm import tqdm
from time import time
from torch.nn import functional as F
import torch.distributed as dist
from torch.nn.parallel import DistributedDataParallel as DDP
from torch.utils.data.distributed import DistributedSampler
# Add project root directory to Python path
project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.append(project_root)

from NN_layers.linear import LinearMem
from pimpy.memmat_tensor import DPETensor

class MNISTClassifier(nn.Module):
    """MLP classifier for MNIST with configurable layers.
    Args:
        engine: Memristive simulation engine
        input_slice: Input tensor slicing configuration
        weight_slice: Weight tensor slicing configuration
        device: Computation device (CPU/GPU)
        layer_dims: List of layer dimensions [input_dim, hidden_dims..., output_dim]
        bw_e: if bw_e is None, the memristive engine is INT mode, otherwise, the memristive engine is FP mode (bw_e is the bitwidth of the exponent)
        mem_enabled: If mem_enabled is True, the model will use the memristive engine for memristive weight updates
    """
    def __init__(self, engine, input_slice, weight_slice, device, 
                 layer_dims=[784, 512, 128, 10], bw_e=None, mem_enabled=True):
        super().__init__()
        self.layers = nn.ModuleList()
        self.flatten = nn.Flatten()
        self.engine = engine
        self.mem_enabled = mem_enabled
        # Create hidden layers
        for in_dim, out_dim in zip(layer_dims[:-1], layer_dims[1:]):
            if mem_enabled is True:
                self.layers.append(
                    LinearMem(engine, in_dim, out_dim, input_slice, weight_slice,
                             device=device, bw_e=bw_e)
                )
            else:
                self.layers.append(nn.Linear(in_dim, out_dim))

    def forward(self, x):
        """Forward pass with ReLU activation and final softmax."""
        x = self.flatten(x)
        for layer in self.layers[:-1]:
            x = F.relu(layer(x))
        x = self.layers[-1](x)
        return F.softmax(x, dim=1)

    def update_weights(self):
        """Update weights for all layers."""
        # 检查模型是否被包裹在 DataParallel 中
        if isinstance(self, nn.DataParallel):
            module = self.module
        else:
            module = self
        
        if module.mem_enabled:
            for layer in module.layers:
                layer.update_weight()
def load_mnist(data_root, batch_size=256):
    """Load MNIST dataset with normalization."""
    transform = transforms.Compose([
        transforms.ToTensor(),
        transforms.Normalize((0.5,), (0.5,))
    ])

    # Create dataset directories if not exist
    os.makedirs(data_root, exist_ok=True)

    train_set = datasets.MNIST(data_root, train=True, download=True, transform=transform)
    test_set = datasets.MNIST(data_root, train=False, download=True, transform=transform)

    # 初始化分布式采样器
    train_sampler = DistributedSampler(train_set)
    test_sampler = DistributedSampler(test_set)

    train_loader = torch.utils.data.DataLoader(
        train_set,
        batch_size=batch_size,
        shuffle=False,
        sampler=train_sampler,
        num_workers=4,
        pin_memory=True
    )
    test_loader = torch.utils.data.DataLoader(
        test_set,
        batch_size=batch_size,
        shuffle=False,
        sampler=test_sampler,
        num_workers=4,
        pin_memory=True
    )
    
    return train_loader, test_loader

def train_model(model, train_loader, test_loader, device, 
                epochs=10, lr=0.001, mem_enabled=True):
    """Train the model with progress tracking and validation.
    
    Args:
        model: Model to train
        train_loader: Training data loader
        test_loader: Test data loader
        device: Computation device
        epochs: Number of training epochs
        lr: Learning rate
        mem_enabled: Enable memory updates
    """
    optimizer = optim.Adam(model.parameters(), lr=lr)
    criterion = nn.CrossEntropyLoss()
    
    for epoch in range(epochs):
        model.train()
        epoch_loss = 0.0
        train_loader.sampler.set_epoch(epoch)
        # Training phase
        with tqdm(train_loader, unit="batch", desc=f"Epoch {epoch+1}/{epochs}") as pbar:
            for images, labels in pbar:
                images, labels = images.to(device), labels.to(device)
                
                optimizer.zero_grad()
                outputs = model(images)
                loss = criterion(outputs, labels)
                loss.backward()
                optimizer.step()
                
                if mem_enabled:
                    model.module.update_weights()
                
                epoch_loss += loss.item() * images.size(0)
                pbar.set_postfix({"loss": f"{loss.item():.4f}"})
        
        # Validation phase
        avg_loss = epoch_loss / len(train_loader.dataset)
        val_acc = evaluate(model, test_loader, device)
        print(f"Epoch {epoch+1} - Avg loss: {avg_loss:.4f}, Val accuracy: {val_acc:.2%}")

def evaluate(model, test_loader, device):
    """Evaluate model performance on test set.
    
    Args:
        model: Model to evaluate
        test_loader: Test data loader
        device: Computation device
        
    Returns:
        Classification accuracy
    """
    model.eval()
    correct = 0
    total = 0
    
    with torch.no_grad():
        for images, labels in tqdm(test_loader, desc="Evaluating", unit="batch"):
            images, labels = images.to(device), labels.to(device)
            outputs = model(images)
            _, predicted = torch.max(outputs.data, 1)
            total += labels.size(0)
            correct += (predicted == labels).sum().item()
    
    return correct / total

def main():
    # Configuration
    config = {
        "data_root": "/dataset/",
        "batch_size": 256,
        "epochs": 10,
        "learning_rate": 0.001,
        "layer_dims": [784, 512, 128, 10],
        "input_slice": (1, 1, 2),
        "weight_slice": (1, 1, 2),
        "bw_e": 8,
    }
    local_rank = int(os.environ["LOCAL_RANK"])
    torch.cuda.set_device(local_rank)
    dist.init_process_group(backend="nccl")

    # Initialize components
    train_loader, test_loader = load_mnist(config["data_root"], config["batch_size"])
    
    # Initialize memory engine and model
    mem_engine = DPETensor(
        var=0.02,
        rdac=2**2,
        g_level=2**2,
        radc=2**12,
        quant_array_gran=(128, 1),
        quant_input_gran=(1, 128),
        paral_array_size=(64, 1),
        paral_input_size=(1, 64),
        device=torch.device("cuda")
    )
    model = MNISTClassifier(
        engine=mem_engine,
        input_slice=config["input_slice"],
        weight_slice=config["weight_slice"],
        device=torch.device("cuda"),
        layer_dims=config["layer_dims"],
        bw_e=config["bw_e"],
        mem_enabled=True
    ).to(local_rank)

    if torch.cuda.device_count() > 1:
        print(f"Using {torch.cuda.device_count()} GPUs!")
        model = DDP(model, device_ids=[local_rank], output_device=local_rank)
    # Train and evaluate
    train_model(
        model,
        train_loader,
        test_loader,
        device=torch.device("cuda"),
        epochs=config["epochs"],
        lr=config["learning_rate"],
        mem_enabled=True
    )
    dist.destroy_process_group()

    # 评估模型
    if local_rank == 0:
        model = model.module  # 获取 DDP 模型的主模块
        model.load_state_dict(model.state_dict())
        model.update_weights()
        final_acc_mem = evaluate(model, test_loader, torch.device("cuda"))
        print(f"\nFinal test accuracy in mem mode: {final_acc_mem:.2%}")

if __name__ == "__main__":
    main()