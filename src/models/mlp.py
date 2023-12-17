import shutil
from typing import List, Dict, Any

import optuna
import pytorch_lightning as pl
from pytorch_lightning.callbacks import ModelCheckpoint
from sklearn.metrics import roc_auc_score
from torch import nn
import torch

from src.datasets.load_tabular_data import TabularDataset


class MLP(nn.Module):
    def __init__(self,
                 input_dim: int,
                 output_dim: int,
                 n_layers: int = 3,
                 hidden_dim: int = 128):
        super(MLP, self).__init__()
        self.input_dim = input_dim
        self.output_dim = output_dim
        self.n_layers = n_layers
        self.hidden_dim = hidden_dim

        # define model's architecture:
        layers = [
            nn.BatchNorm1d(input_dim),  # TODO discard affine=False # normalize input
            nn.Linear(input_dim, hidden_dim),  # first layer
            nn.ReLU(),
        ]
        for _ in range(n_layers - 1):
            layers += [
                nn.Linear(hidden_dim, hidden_dim),
                nn.ReLU()
            ]
        layers += [
            nn.Linear(hidden_dim, output_dim)  # final layer
        ]
        self.model = nn.Sequential(*layers)

    def forward(self, x):
        logits = self.model(x)
        return logits


# TODO currently the main metric is auc, make it configurable?
# Wrapping with torch lightning functionality:
class LitMLP(pl.LightningModule):
    """ Defined the torch lightning system, the wraps the torch module (MLP) """

    def __init__(self,
                 input_dim: int,
                 output_dim: int,

                 # Architecture HPs:
                 n_layers: int = 3,
                 hidden_dim: int = 128,

                 # Optimization HPs
                 lr=1e-3,
                 weight_decay=1e-5,

                 **kwargs):
        super().__init__()
        self.save_hyperparameters()
        self.model = MLP(input_dim, output_dim, n_layers, hidden_dim)
        self.loss = nn.CrossEntropyLoss()

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        return self.model(x)

    def training_step(self, batch: List[torch.Tensor], batch_idx: int) -> torch.Tensor:
        x, y = batch
        logits = self(x)
        self.evaluate(batch, logits=logits, stage='train')
        return self.loss(logits, y)

    def evaluate(self, batch, stage=None, logits=None):
        x, y = batch
        if logits is None:
            logits = self(x)
        loss = self.loss(logits, y)
        pred = logits.argmax(dim=1, keepdim=True)
        accuracy = pred.eq(y.view_as(pred)).float().mean()
        auc = roc_auc_score(y.cpu(), pred.cpu())
        self.log(f"{stage}_acc", accuracy)
        self.log(f"{stage}_loss", loss)
        self.log(f"{stage}_auc", auc)

        # metric aggregated across epoch:
        # 'hp_metric' is the metric to be optimized for hps tuning
        self.log(f"{stage}_hp_metric", auc, on_step=False, on_epoch=True)

    def validation_step(self, batch, batch_idx):
        self.evaluate(batch, stage="val")

    def test_step(self, batch, batch_idx):
        self.evaluate(batch, stage="test")

    def configure_optimizers(self) -> torch.optim.Optimizer:
        return torch.optim.Adam(self.model.parameters(),
                                lr=self.hparams.lr,
                                weight_decay=self.hparams.weight_decay)

    @classmethod
    def define_trial_parameters(cls, trial: optuna.trial.Trial) -> Dict[str, Any]:
        """
        Define the hyperparameters and their ranges to be optimized by optuna.
        """
        return dict(
            n_layers=trial.suggest_int("n_layers", 2, 5),
            hidden_dim=trial.suggest_int("hidden_dim", 32, 512),
            lr=trial.suggest_float("lr", 1e-7, 1e-1, log=True),
            weight_decay=trial.suggest_float("weight_decay", 1e-6, 1e-1, log=True),
        )


def train(hyperparameters,
          data_parameters,
          model_artifact_path,
          additional_callbacks=None):
    """
    Train a lightning-based model (using lightning Trainer API) with the given hyperparameters.
    """

    # TODO decouple data out of this function?
    # Load dataset:
    tab_dataset = TabularDataset(**data_parameters)
    trainset, testset = tab_dataset.trainset, tab_dataset.testset
    hyperparameters['data_parameters'] = data_parameters

    # Setup data loaders:
    trainloader = torch.utils.data.DataLoader(trainset, batch_size=2048, shuffle=True)
    testloader = torch.utils.data.DataLoader(testset, batch_size=2048, shuffle=False)

    # Define the model
    model = LitMLP(input_dim=tab_dataset.n_features, output_dim=tab_dataset.n_classes,
                   **hyperparameters)

    # Define callbacks:
    callbacks = []
    # defines checkpointing at the end of each epoch, saving the max-validation-metric model
    callbacks.append(ModelCheckpoint(monitor="val_hp_metric", mode="max",
                                     filename='{epoch}-{val_hp_metric:.3f}'
                                     ))

    if additional_callbacks is not None:
        callbacks += additional_callbacks

    # define the trainer:
    trainer = pl.Trainer(
        max_epochs=30,
        callbacks=callbacks,
        default_root_dir=f"outputs/training/mlps/{data_parameters['dataset_name']}/",  # TODO configurable

        # Default configs:
        # accelerator="auto",
        # devices="auto",
        # logger=True, # tensorboard if available, otherwise csv
    )
    trainer.logger.log_hyperparams(hyperparameters)
    trainer.fit(model, train_dataloaders=trainloader, val_dataloaders=testloader)

    results = {
        'best_val_loss': trainer.callback_metrics['val_loss'].item(),
        'best_val_acc': trainer.callback_metrics['val_acc'].item(),
        'best_val_hp_metric': trainer.callback_metrics['val_hp_metric'].item(),

        'best_model_path': trainer.checkpoint_callback.best_model_path,
        'best_model_val_hp_metric': trainer.checkpoint_callback.best_model_score,
    }

    shutil.copy(trainer.checkpoint_callback.best_model_path, model_artifact_path)

    print(results)
    return results


def grid_search_hyperparameters(data_parameters):
    """
    Runs hyperparameters tuning using optuna.
    :return: the best parameters found
    """
    def optuna_hpo_objective(trial: optuna.trial.Trial) -> float:
        """
        Wraps the training to set the specific objective for optuna to optimize.
        """
        # suggested HPs dict:
        hyperparameters = LitMLP.define_trial_parameters(trial)
        # train the model with the suggested HPs:
        results = train(hyperparameters,
                        data_parameters,
                        additional_callbacks=[
                            optuna.integration.PyTorchLightningPruningCallback(trial, monitor="val_hp_metric")
                        ])

        return results['best_model_val_hp_metric']

    pruner = optuna.pruners.MedianPruner()  # if args.pruning else optuna.pruners.NopPruner()
    study = optuna.create_study(direction="maximize", pruner=pruner)
    study.optimize(optuna_hpo_objective, n_trials=100, timeout=600)

    return study.best_trial.params


if __name__ == '__main__':
    train(
        dict(
            n_layers=3,
            hidden_dim=128,
            lr=0.001,
            weight_decay=1e-05,
        )
    )
