import argparse
import wandb

parser = argparse.ArgumentParser()
parser.add_argument('task', metavar='TASK', choices=[
        'PSR', 'RSR', 'PPI', 'RES', 'MSP', 'SMP', 'LBA', 'LEP'
    ], help="{PSR, RSR, PPI, RES, MSP, SMP, LBA, LEP}")
parser.add_argument('--num-workers', metavar='N', type=int, default=4,
                   help='number of threads for loading data, default=4')
parser.add_argument('--smp-idx', metavar='IDX', type=int, default=None,
                   choices=list(range(20)),
                   help='label index for SMP, in range 0-19')
parser.add_argument('--lba-split', metavar='SPLIT', type=int, choices=[30, 60],
                    help='identity cutoff for LBA, 30 (default) or 60', default=30)
parser.add_argument('--batch', metavar='SIZE', type=int, default=8,
                    help='batch size, default=8')
parser.add_argument('--train-time', metavar='MINUTES', type=int, default=120,
                    help='maximum time between evaluations on valset, default=120 minutes')
parser.add_argument('--val-time', metavar='MINUTES', type=int, default=20,
                    help='maximum time per evaluation on valset, default=20 minutes')
parser.add_argument('--epochs', metavar='N', type=int, default=50,
                    help='training epochs, default=50')
parser.add_argument('--test', metavar='PATH', default=None,
                    help='evaluate a trained model')
parser.add_argument('--lr', metavar='RATE', default=1e-4, type=float,
                    help='learning rate')
parser.add_argument('--load', metavar='PATH', default=None, 
                    help='initialize first 2 GNN layers with pretrained weights')

args = parser.parse_args()

import gvp
from atom3d.datasets import LMDBDataset
import torch_geometric
from functools import partial
import gvp.atom3d
import torch.nn as nn
import tqdm, torch, time, os
import numpy as np
from atom3d.util import metrics
import sklearn.metrics as sk_metrics
from collections import defaultdict
import scipy.stats as stats
print = partial(print, flush=True)

models_dir = '/home/wpan/gvp-pytorch/models'
device = 'cuda' if torch.cuda.is_available() else 'cpu'
model_id = float(time.time())

# Start a new wandb run to track this script.
run = wandb.init(
    # Set the wandb project where this run will be logged.
    project="res",
    # Track hyperparameters and run metadata.
    config={
        "learning_rate": args.lr,
        "architecture": "ResGvp",
        "dataset": "RES",
        "epochs": args.epochs,
    },
)

def main():
    datasets = get_datasets(args.task, args.lba_split)
    dataloader = partial(torch_geometric.data.DataLoader, 
                    num_workers=args.num_workers, batch_size=args.batch)
    if args.task not in ['PPI', 'RES']:
        dataloader = partial(dataloader, shuffle=True)
        
    trainset, valset, testset = map(dataloader, datasets)    
    model = get_model(args.task).to(device)
    
    if args.test:
        test(model, testset)

    else:
        if args.load:
            load(model, args.load)
        train(model, trainset, valset)
        
def test(model, testset):
    model.load_state_dict(torch.load(args.test))
    model.eval()
    t = tqdm.tqdm(testset)
    metrics = get_metrics(args.task)
    targets, predicts, ids = [], [], []
    with torch.no_grad():
        for batch in t:
            pred = forward(model, batch, device)
            label = get_label(batch, args.task, args.smp_idx)
            if args.task == 'RES':
                pred = pred.argmax(dim=-1)
            if args.task in ['PSR', 'RSR']:
                ids.extend(batch.id)
            targets.extend(list(label.cpu().numpy()))
            predicts.extend(list(pred.cpu().numpy()))

    for name, func in metrics.items():
        if args.task in ['PSR', 'RSR']:
            func = partial(func, ids=ids)
        value = func(targets, predicts)
        print(f"{name}: {value}")
        
        # Log metrics to wandb.
        run.log({
            f"{name}": value,
        })

def train(model, trainset, valset):
                                
    optimizer = torch.optim.Adam(model.parameters(), lr=args.lr)
    
    best_path, best_val = None, np.inf
    
    for epoch in range(args.epochs):
        model.train()
        train_loss, train_acc = loop(trainset, model, optimizer=optimizer, max_time=args.train_time)
        path = f"{models_dir}/{args.task}_{model_id}_{epoch}.pt"
        torch.save(model.state_dict(), path)
        print(f'\nEPOCH {epoch} TRAIN loss: {train_loss:.8f} acc: {train_acc:.2f}%')
        model.eval()
        with torch.no_grad():
            val_loss, val_acc = loop(valset, model, max_time=args.val_time)
        print(f'\nEPOCH {epoch} VAL loss: {val_loss:.8f} acc: {val_acc:.2f}%')
        if val_loss < best_val:
            best_path, best_val = path, val_loss
        print(f'BEST {best_path} VAL loss: {best_val:.8f}')
        
        # Log metrics to wandb.
        run.log({
            "train_loss": train_loss,
            "train_acc": train_acc,
            "val_loss": val_loss,
            "val_acc": val_acc
        })
    
def loop(dataset, model, optimizer=None, max_time=None):
    start = time.time()
    
    loss_fn = get_loss(args.task)
    t = tqdm.tqdm(dataset)
    metrics = get_metrics(args.task)
    total_loss, total_count = 0, 0
    targets, predicts = [], []
    
    for batch in t:
        if max_time and (time.time() - start) > 60*max_time: break
        
        # Start timing this batch
        batch_start_time = time.time()
        
        if optimizer: optimizer.zero_grad()
        try:
            out = forward(model, batch, device)
        except RuntimeError as e:
            if "CUDA out of memory" not in str(e): raise(e)
            torch.cuda.empty_cache()
            print('Skipped batch due to OOM', flush=True)
            continue
            
        label = get_label(batch, args.task, args.smp_idx)
        loss_value = loss_fn(out, label)
        total_loss += float(loss_value)
        total_count += 1
        
        # Get predictions
        pred_class = out.argmax(dim=-1)
        targets.extend(label.cpu().tolist())
        predicts.extend(pred_class.cpu().tolist())
        
        if optimizer:
            try:
                loss_value.backward()
                optimizer.step()
            except RuntimeError as e:
                if "CUDA out of memory" not in str(e): raise(e)
                torch.cuda.empty_cache()
                print('Skipped batch due to OOM', flush=True)
                continue
            
        # Calculate and print the time taken for this batch
        batch_time = time.time() - batch_start_time
        print(f"Batch {total_count} processing time: {batch_time:.4f} seconds")
            
        t.set_description(f"{total_loss/total_count:.8f}")
        
        # Log metrics to wandb.
        run.log({
            "loss": total_loss / total_count,
            "accuracy": metrics['accuracy'](targets, predicts),
            "batch_time": batch_time
        })

    accuracy = metrics['accuracy'](targets, predicts)
    return total_loss / total_count, accuracy

def load(model, path):
    params = torch.load(path)
    state_dict = model.state_dict()
    for name, p in params.items():
        if name in state_dict and \
               name[:8] in ['layers.0', 'layers.1'] and \
               state_dict[name].shape == p.shape:
            print("Loading", name)
            model.state_dict()[name].copy_(p)
        
#######################################################################

def get_label(batch, task, smp_idx=None):
    if type(batch) in [list, tuple]: batch = batch[0]
    if task == 'SMP':
        assert smp_idx is not None
        return batch.label[smp_idx::20]
    return batch.label

def get_metrics(task):
    def _correlation(metric, targets, predict, ids=None, glob=True):
        if glob: return metric(targets, predict)
        _targets, _predict = defaultdict(list), defaultdict(list)
        for _t, _p, _id in zip(targets, predict, ids):
            _targets[_id].append(_t)
            _predict[_id].append(_p)
        return np.mean([metric(_targets[_id], _predict[_id]) for _id in _targets])
        
    correlations = {
        'pearson': partial(_correlation, metrics.pearson),
        'kendall': partial(_correlation, metrics.kendall),
        'spearman': partial(_correlation, metrics.spearman)
    }
    mean_correlations = {f'mean {k}' : partial(v, glob=False) \
                            for k, v in correlations.items()}
    
    return {                       
        'RSR' : {**correlations, **mean_correlations},
        'PSR' : {**correlations, **mean_correlations},
        'PPI' : {'auroc': metrics.auroc},
        'RES' : {'accuracy': metrics.accuracy},
        'MSP' : {'auroc': metrics.auroc, 'auprc': metrics.auprc},
        'LEP' : {'auroc': metrics.auroc, 'auprc': metrics.auprc},
        'LBA' : {**correlations, 'rmse': partial(sk_metrics.mean_squared_error, squared=False)},
        'SMP' : {'mae': sk_metrics.mean_absolute_error}
    }[task]
            
def get_loss(task):
    if task in ['PSR', 'RSR', 'SMP', 'LBA']: return nn.MSELoss() # regression
    elif task in ['PPI', 'MSP', 'LEP']: return nn.BCELoss() # binary classification
    elif task in ['RES']: return nn.CrossEntropyLoss() # multiclass classification
    
def forward(model, batch, device):
    if type(batch) in [list, tuple]:
        batch = batch[0].to(device), batch[1].to(device)
    else:
        batch = batch.to(device)
    return model(batch)

def get_datasets(task, lba_split=30):
    data_path = {
        'RES' : 'raw/RES/data/',
        'PPI' : 'atom3d-data/PPI/splits/DIPS-split/data/',
        'RSR' : 'atom3d-data/RSR/splits/candidates-split-by-time/data/',
        'PSR' : 'atom3d-data/PSR/splits/split-by-year/data/',
        'MSP' : 'atom3d-data/MSP/splits/split-by-sequence-identity-30/data/',
        'LEP' : 'atom3d-data/LEP/splits/split-by-protein/data/',
        'LBA' : f'atom3d-data/LBA/splits/split-by-sequence-identity-{lba_split}/data/',
        'SMP' : 'atom3d-data/SMP/splits/random/data/'
    }[task]
        
    if task == 'RES':
        split_path = 'indices/'
        dataset = partial(gvp.atom3d.RESDataset, data_path)        
        trainset = dataset(split_path=split_path+'train_indices.txt')
        valset = dataset(split_path=split_path+'val_indices.txt')
        testset = dataset(split_path=split_path+'test_indices.txt')
    
    elif task == 'PPI':
        trainset = gvp.atom3d.PPIDataset(data_path+'train')
        valset = gvp.atom3d.PPIDataset(data_path+'val')
        testset = gvp.atom3d.PPIDataset(data_path+'test')
        
    else:
        transform = {                       
            'RSR' : gvp.atom3d.RSRTransform,
            'PSR' : gvp.atom3d.PSRTransform,
            'MSP' : gvp.atom3d.MSPTransform,
            'LEP' : gvp.atom3d.LEPTransform,
            'LBA' : gvp.atom3d.LBATransform,
            'SMP' : gvp.atom3d.SMPTransform,
        }[task]()
        
        trainset = LMDBDataset(data_path+'train', transform=transform)
        valset = LMDBDataset(data_path+'val', transform=transform)
        testset = LMDBDataset(data_path+'test', transform=transform)
        
    return trainset, valset, testset

def get_model(task):
    return {
        'RES' : gvp.atom3d.RESModel,
        'PPI' : gvp.atom3d.PPIModel,
        'RSR' : gvp.atom3d.RSRModel,
        'PSR' : gvp.atom3d.PSRModel,
        'MSP' : gvp.atom3d.MSPModel,
        'LEP' : gvp.atom3d.LEPModel,
        'LBA' : gvp.atom3d.LBAModel,
        'SMP' : gvp.atom3d.SMPModel
    }[task]()

if __name__ == "__main__":
    main()
    # Finish the run and upload any remaining data.
    run.finish()