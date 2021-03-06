from argparse import ArgumentParser
from mach_utils import get_config, create_record_dir, mkdir, evaluate_single, get_model_dir, get_label_hash, \
    get_mapped_labels, log_eval_results, AverageMeter
import os
from fc_network import FCNetwork
import torch
import tqdm
import logging
from dataset import XCDataset
import time
from xclib.data import data_utils
from collections import Counter
import visdom
import numpy as np


def get_args():
    p = ArgumentParser()
    p.add_argument("--rep", '-r', dest="rep", type=int, default=0,
                   help="Which reptition to train. Default 0.")
    p.add_argument("--model", '-m', dest="model", type=str, required=True,
                   help="Path to the model config yaml file.")
    p.add_argument("--dataset", '-d', dest="dataset", type=str, required=True,
                   help="Path to the data config yaml file.")
    p.add_argument("--gpus", '-g', dest="gpus", type=str, required=False, default="0",
                   help="A string that specifies which GPU you want to use, split by comma. Eg 0,1. Default 0.")
    p.add_argument("--cost", '-c', dest="cost", type=str, required=False, default='',
                   help="Use cost-sensitive training or not. Should be in [hashed, original]. "
                   "Default empty string, which indicates that no cost-sensitive is used.")
    p.add_argument("--vis", '-v', dest="vis", action='store_true',
                   help="Plot loss values against epochs.")

    return p.parse_args()


def get_hashed_label_weight(label_mapping, b):
    features, labels, num_samples, num_features, num_labels = data_utils.read_data(
        train_file)
    instance, labels_flatten = labels.nonzero()
    mapped_labels = label_mapping[labels_flatten]
    m_count = Counter(mapped_labels)
    m_count_tensor = torch.zeros(b)
    for k, v in m_count.items():
        m_count_tensor[k] = v
    total = m_count_tensor.sum()
    rest = total-m_count_tensor
    m_count_tensor[m_count_tensor ==
                   0] = m_count_tensor[m_count_tensor != 0] .min()
    # m_count_tensor[m_count_tensor == 0] = float("-inf")
    w = rest / m_count_tensor
    # w = w / w.min()
    return w


def train(data_cfg, model_cfg, a, gpus, train_loader, val_loader):
    """
        Train one division
    """
    rep = a.rep
    cuda = torch.cuda.is_available()
    name = data_cfg['name']
    prefix = data_cfg['prefix']
    ori_dim = data_cfg['ori_dim']
    dest_dim = model_cfg['dest_dim']
    b = model_cfg['b']
    R = model_cfg['r']
    # each repetition has its own dir
    model_dir = get_model_dir(data_cfg, model_cfg, a)
    mkdir(model_dir)
    latest_param = os.path.join(model_dir, model_cfg["latest_file"])
    best_score = -float('inf')
    best_param = os.path.join(model_dir, model_cfg["best_file"])

    # logger
    log_file = "train.log"
    # print to log file as well as stdout
    logging.basicConfig(level=logging.INFO,
                        format='%(asctime)s %(levelname)-8s %(message)s', datefmt='%Y-%m-%d %H:%M:%S',
                        handlers=[
                            logging.FileHandler(
                                os.path.join(model_dir, log_file)),
                            logging.StreamHandler()
                        ])
    # build model and optimizers

    layers = [dest_dim] + model_cfg['hidden'] + [b]
    model = FCNetwork(layers)
    model = torch.nn.DataParallel(model, device_ids=gpus)
    if cuda:
        model = model.cuda()
    opt = torch.optim.Adam(model.parameters(), lr=model_cfg['lr'])
    lr_sch = torch.optim.lr_scheduler.MultiStepLR(
        opt, model_cfg['lr_step'], model_cfg['lr_factor'])
    # load pretrained parameters/checkpoint
    begin = model_cfg['begin_epoch']
    preload_path = model_cfg["pretrained"] if model_cfg["pretrained"] else latest_param
    if os.path.exists(preload_path):
        meta_info = torch.load(preload_path)
        if cuda:
            meta_info = torch.load(preload_path)
        else:
            meta_info = torch.load(
                preload_path, map_location=lambda storage, loc: storage)
        opt.load_state_dict(meta_info['opt'])
        lr_sch.load_state_dict(meta_info['lr_sch'])
        begin = meta_info['epoch']
        best_score = meta_info["best_score"]
    end = model_cfg['end_epoch']
    # load mapping
    ori_labels = data_cfg['num_labels']

    record_dir = data_cfg["record_dir"]
    label_path = os.path.join(record_dir, "_".join(
        [prefix, str(ori_labels), str(b), str(R)]))  # Bibtex_159_100_32
    _, label_mapping, _ = get_label_hash(label_path, rep)
    if a.cost == 'hashed':
        weights = get_hashed_label_weight(label_mapping, b)
    else:
        weights = torch.ones(b)
    if cuda:
        weights = weights.cuda()
    label_mapping = torch.from_numpy(label_mapping)
    loss_func = torch.nn.BCEWithLogitsLoss(weight=weights)
    loss_meter = AverageMeter()
    # train
    for ep in tqdm.tqdm(range(begin, end)):
        model.train()
        start = time.perf_counter()
        loss_meter.reset()

        for sample in train_loader:
            X, y = sample
            # TODO: Check if it is better to unfold the neural network manually using sparse vectors,
            #  or just make vectors dense
            X = X.to_dense()
            y = get_mapped_labels(y, label_mapping, b)
            if cuda:
                X = X.cuda()
                y = y.cuda()
            opt.zero_grad()
            out = model(X)
            loss = loss_func(out, y)
            loss.backward()
            loss_meter.update(loss, y.shape[0])
            opt.step()
            lr_sch.step(ep)
        end = time.perf_counter()

        logging.info("-----Rep %d, Ep %d-------" % (rep, ep))
        logging.info("Training Time Elapsed: %.3f s." % (end - start))

        # logging.info("Epoch %d" % (ep))
        # logging.info("EVALUATION ON TRAIN SET")
        # loss, train_d, mAP = evaluate_single(model, train_loader, model_cfg, label_mapping)
        # log_eval_results(train_d)
        # logging.info("Loss ON TRAIN SET: %.3f, mAP: %.3f" % (loss, mAP))

        logging.info("EVALUATION ON VAL SET")
        l, val_d, m = evaluate_single(
            model, val_loader, model_cfg, label_mapping, weights)
        log_eval_results(val_d)
        logging.info("Loss ON VAL SET: %.3f, mAP: %.3f" % (l, m))

        if best_score < m:
            best_score = m
            is_best = True
            logging.warning("A NEW RECORD! {:.3f}".format(m))
        else:
            is_best = False
        # might be cuda, might not be cuda, please make sure it is consistent
        logging.info("-----------------")

        ckpt = {
            "opt": opt.state_dict(),
            "lr_sch": lr_sch.state_dict(),
            "epoch": ep+1,
            "val_map": m,
            "model": model.state_dict(),
            "best_score": best_score,  # best score till now
            "metrics": val_d,
        }

        start = time.perf_counter()
        logging.info("Saving models...")
        torch.save(ckpt, latest_param)
        if is_best:
            torch.save(ckpt, best_param)
        end = time.perf_counter()
        logging.info("Model Saved. Time Elapsed: %.3f s." % (end - start))

        if viz is not None:
            viz.line(X=np.array([ep+1]), Y=np.array([loss_meter.avg.item()]),
                     win=line, update='append')


if __name__ == "__main__":
    a = get_args()
    data_cfg = get_config(a.dataset)
    model_cfg = get_config(a.model)
    create_record_dir(data_cfg)
    # load dataset
    gpus = [int(i) for i in a.gpus.split(",")]

    name = data_cfg['name']
    prefix = data_cfg['prefix']
    data_dir = os.path.join("data", name)
    train_file = prefix + "_" + "train.txt"
    train_file = os.path.join(data_dir, train_file)
    train_set = XCDataset(train_file, a.rep, data_cfg, model_cfg, 'tr')
    val_set = XCDataset(train_file, a.rep, data_cfg, model_cfg, 'val')
    train_loader = torch.utils.data.DataLoader(
        train_set, batch_size=model_cfg['batch_size'], shuffle=model_cfg['shuffle'])
    val_loader = torch.utils.data.DataLoader(
        val_set, batch_size=model_cfg['batch_size'])
    viz = None
    line = None
    if a.vis:
        viz = visdom.Visdom()
        line = viz.line(
            X=np.array([0]),
            Y=np.array([0]),
            opts=dict(title=get_model_dir(data_cfg, model_cfg, a)))
    train(data_cfg, model_cfg, a, gpus, train_loader, val_loader)
