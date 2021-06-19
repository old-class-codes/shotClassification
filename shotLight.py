import glob
import json
import os
import pickle
from collections import Counter
from pathlib import Path

import albumentations as A
import cv2
import matplotlib.pyplot as plt
import numpy as np
import optuna
import pandas as pd
import pytorch_lightning as pl
import torch
import torch.optim as optim
import torchvision
from albumentations.core.composition import Compose
from albumentations.pytorch import ToTensorV2
from efficientnet_pytorch import EfficientNet
from optuna.integration import PyTorchLightningPruningCallback
from pytorch_lightning import Trainer
from pytorch_lightning.loggers import CSVLogger
from pytorch_lightning.metrics.functional import accuracy
from sklearn import metrics, model_selection, preprocessing
from sklearn.model_selection import StratifiedKFold
from torch import nn
from torch.nn import functional as F
from torch.utils.data import DataLoader, Dataset, TensorDataset, random_split
from torchvision import datasets, transforms
from torchvision.datasets import ImageFolder

os.environ["TORCH_HOME"] = "/media/hdd/Datasets/"
# -

import torchsnooper as sn

# # Look at data
# - Create a csv for easy loading

main_path = "/media/hdd/Datasets/shotclassification/trailer/"

all_ims = glob.glob(main_path + "/*/*.jpg")
all_ims[0]

print(len(all_ims))


def create_label(x):
    return str(Path(Path(x).name.split("_")[-1]).stem)


df = pd.DataFrame.from_dict(
    {x: create_label(x) for x in all_ims}, orient="index"
).reset_index()

df.columns = ["image_id", "label"]

print(df.head())

temp = preprocessing.LabelEncoder()
df["label"] = temp.fit_transform(df.label.values)

label_map = {i: l for i, l in enumerate(temp.classes_)}

df.label.nunique()

df.label.value_counts()

df["kfold"] = -1
df = df.sample(frac=1).reset_index(drop=True)
stratify = StratifiedKFold(n_splits=5)
for i, (t_idx, v_idx) in enumerate(
    stratify.split(X=df.image_id.values, y=df.label.values)
):
    df.loc[v_idx, "kfold"] = i
    df.to_csv("train_folds.csv", index=False)

print(pd.read_csv("train_folds.csv").head(1))


# # Create model

# +
# Efficient net b5
# @sn.snoop()
class LitModel(pl.LightningModule):
    def __init__(self, num_classes, learning_rate=1e-4, weight_decay=0.0001):
        super().__init__()

        # log hyperparameters
        self.save_hyperparameters()
        self.num_classes = num_classes

        self.learning_rate = learning_rate
        self.weight_decay = weight_decay

        self.enet = EfficientNet.from_pretrained(
            "efficientnet-b3", num_classes=self.num_classes
        )
        in_features = self.enet._fc.in_features
        self.enet._fc = nn.Linear(in_features, num_classes)

    #     @sn.snoop()

    def forward(self, x):
        out = self.enet(x)
        return out

    def configure_optimizers(self):
        optimizer = torch.optim.AdamW(
            self.parameters(), lr=self.learning_rate, weight_decay=self.weight_decay
        )

        scheduler = torch.optim.lr_scheduler.StepLR(optimizer, step_size=2, gamma=0.1)

        return ([optimizer], [scheduler])

    def training_step(self, train_batch, batch_idx):
        x, y = train_batch["x"], train_batch["y"]
        preds = self(x)
        loss = F.cross_entropy(preds, y)
        #         loss.requires_grad = True
        acc = accuracy(preds, y)
        self.log("train_acc_step", acc)
        self.log("train_loss", loss)
        return loss

    def validation_step(self, val_batch, batch_idx):
        x, y = val_batch["x"], val_batch["y"]
        preds = self(x)
        loss = F.cross_entropy(preds, y)
        #         loss.requires_grad = True
        acc = accuracy(preds, y)
        self.log("val_acc_step", acc)
        self.log("val_loss", loss)


# -


class ImageClassDs(Dataset):
    def __init__(
        self, df: pd.DataFrame, imfolder: str, train: bool = True, transforms=None
    ):
        self.df = df
        self.imfolder = imfolder
        self.train = train
        self.transforms = transforms

    def __getitem__(self, index):
        im_path = self.df.iloc[index]["image_id"]
        x = cv2.imread(im_path, cv2.IMREAD_COLOR)
        x = cv2.cvtColor(x, cv2.COLOR_BGR2RGB)

        if self.transforms:
            x = self.transforms(image=x)["image"]

        y = self.df.iloc[index]["label"]
        return {
            "x": x,
            "y": y,
        }

    def __len__(self):
        return len(self.df)


# # Load data


class ImDataModule(pl.LightningDataModule):
    def __init__(
        self,
        df,
        batch_size,
        num_classes,
        data_dir: str = "/media/hdd/Datasets/asl/",
        img_size=(256, 256),
    ):
        super().__init__()
        self.df = df
        self.data_dir = data_dir
        self.batch_size = batch_size
        self.train_transform = A.Compose(
            [
                A.RandomResizedCrop(img_size, img_size, p=1.0),
                A.Transpose(p=0.5),
                A.HorizontalFlip(p=0.5),
                A.VerticalFlip(p=0.5),
                A.ShiftScaleRotate(p=0.5),
                A.HueSaturationValue(
                    hue_shift_limit=0.2, sat_shift_limit=0.2, val_shift_limit=0.2, p=0.5
                ),
                A.RandomBrightnessContrast(
                    brightness_limit=(-0.1, 0.1), contrast_limit=(-0.1, 0.1), p=0.5
                ),
                A.Normalize(
                    mean=[0.485, 0.456, 0.406],
                    std=[0.229, 0.224, 0.225],
                    max_pixel_value=255.0,
                    p=1.0,
                ),
                A.CoarseDropout(p=0.5),
                A.Cutout(p=0.5),
                ToTensorV2(p=1.0),
            ],
            p=1.0,
        )

        self.valid_transform = A.Compose(
            [
                A.CenterCrop(img_size, img_size, p=1.0),
                A.Resize(img_size, img_size),
                A.Normalize(
                    mean=[0.485, 0.456, 0.406],
                    std=[0.229, 0.224, 0.225],
                    max_pixel_value=255.0,
                    p=1.0,
                ),
                ToTensorV2(p=1.0),
            ],
            p=1.0,
        )

    def setup(self, stage=None):
        dfx = pd.read_csv("./train_folds.csv")
        train = dfx.loc[dfx["kfold"] != 1]
        val = dfx.loc[dfx["kfold"] == 1]

        self.train_dataset = ImageClassDs(
            train, self.data_dir, train=True, transforms=self.train_transform
        )

        self.valid_dataset = ImageClassDs(
            val, self.data_dir, train=False, transforms=self.valid_transform
        )

    def train_dataloader(self):
        return DataLoader(
            self.train_dataset, batch_size=self.batch_size, num_workers=12, shuffle=True
        )

    def val_dataloader(self):
        return DataLoader(
            self.valid_dataset, batch_size=self.batch_size, num_workers=12
        )


batch_size = 128
num_classes = 5
img_size = 128
n_epochs = 5

dm = ImDataModule(df, batch_size=batch_size, num_classes=num_classes, img_size=img_size)
class_ids = dm.setup()

# # Logs

model = LitModel(num_classes)

logger = CSVLogger("logs", name="eff-b5")

trainer = pl.Trainer(
    auto_select_gpus=True,
    gpus=1,
    precision=16,
    profiler=False,
    max_epochs=n_epochs,
    callbacks=[pl.callbacks.ProgressBar()],
    automatic_optimization=True,
    enable_pl_optimizer=True,
    logger=logger,
    accelerator="ddp",
    plugins="ddp_sharded",
)

trainer.fit(model, dm)

# +
trainer.test()

trainer.save_checkpoint("model1.ckpt")
# -
