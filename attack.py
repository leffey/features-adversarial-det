import argparse
import os

import matplotlib.pyplot as plt
import numpy as np
import tensorflow as tf
import torch
from cleverhans.attacks import FastGradientMethod, BasicIterativeMethod, MadryEtAl, MomentumIterativeMethod, DeepFool, \
    CarliniWagnerL2, SaliencyMapMethod, LBFGS
from cleverhans.model import CallableModelWrapper
from torch.utils.data import DataLoader
from torchvision import models
from torchvision.transforms import ToTensor, Compose, Resize, Normalize, CenterCrop
from tqdm import tqdm

from utils import ImageDataset, convert_pytorch_model_to_tf


def imshow(inp, title=None):
    """Imshow for Tensor."""
    inp = inp.numpy().transpose((1, 2, 0))
    mean = np.array([0.485, 0.456, 0.406])
    std = np.array([0.229, 0.224, 0.225])
    inp = std * inp + mean
    inp = np.clip(inp, 0, 1)
    plt.imshow(inp)
    if title is not None:
        plt.title(title)
    plt.pause(0.001)


def main(args):
    normalize = Normalize([0.485, 0.456, 0.406], [0.229, 0.224, 0.225])
    transform = Compose([Resize(256),
                         CenterCrop(224),
                         ToTensor(),
                         normalize
                         ])

    dataset = ImageDataset(args.image_folder, transform=transform, return_paths=True)
    # n_images = len(dataset)
    dataloader = DataLoader(dataset, shuffle=False, batch_size=args.batch_size, pin_memory=True, num_workers=0)

    model = models.resnet50(pretrained=True).to(args.device)
    model.eval()

    config = tf.ConfigProto(intra_op_parallelism_threads=1, inter_op_parallelism_threads=1, allow_soft_placement=True,
                            device_count={'CPU': 1})
    sess = tf.Session(config=config)
    x_op = tf.placeholder(tf.float32, shape=(None, 3, 224, 224,))

    tf_model = convert_pytorch_model_to_tf(model, args.device)
    cleverhans_model = CallableModelWrapper(tf_model, output_layer='logits')

    # compute clip_min and clip_max suing a full black and a full white image
    clip_min = normalize(torch.zeros(3, 1, 1)).min().item()
    clip_max = normalize(torch.ones(3, 1, 1)).max().item()

    eps = args.eps / 255.
    eps_iter = 20
    nb_iter = 10
    args.ord = np.inf if args.ord < 0 else args.ord
    grad_params = {'eps': eps, 'ord': args.ord}
    common_params = {'clip_min': clip_min, 'clip_max': clip_max}
    iter_params = {'eps_iter': eps_iter / 255., 'nb_iter': nb_iter}

    attack_name = ''
    if args.attack == 'fgsm':
        attack_name = '_L{}_eps{}'.format(args.ord, args.eps)
        attack_op = FastGradientMethod(cleverhans_model, sess=sess)
        attack_params = {**common_params, **grad_params}
    elif args.attack == 'iter':
        attack_name = '_L{}_eps{}_epsi{}_i{}'.format(args.ord, args.eps, eps_iter, nb_iter)
        attack_op = BasicIterativeMethod(cleverhans_model, sess=sess)
        attack_params = {**common_params, **grad_params, **iter_params}
    elif args.attack == 'm-iter':
        attack_name = '_L{}_eps{}_epsi{}_i{}'.format(args.ord, args.eps, eps_iter, nb_iter)
        attack_op = MomentumIterativeMethod(cleverhans_model, sess=sess)
        attack_params = {**common_params, **grad_params, **iter_params}
    elif args.attack == 'pgd':
        attack_name = '_L{}_eps{}_epsi{}_i{}'.format(args.ord, args.eps, eps_iter, nb_iter)
        attack_op = MadryEtAl(cleverhans_model, sess=sess)
        attack_params = {**common_params, **grad_params, **iter_params}
    elif args.attack == 'jsma':
        attack_op = SaliencyMapMethod(cleverhans_model, sess=sess)
        attack_params = {'theta': eps, 'symbolic_impl': False, **common_params}
    elif args.attack == 'deepfool':
        attack_op = DeepFool(cleverhans_model, sess=sess)
        attack_params = common_params
    elif args.attack == 'cw':
        attack_op = CarliniWagnerL2(cleverhans_model, sess=sess)
        attack_params = common_params
    elif args.attack == 'lbfgs':
        attack_op = LBFGS(cleverhans_model, sess=sess)
        target = np.zeros((1, 1000))
        target[0, np.random.randint(1000)] = 1
        y = tf.placeholder(tf.float32, target.shape)
        attack_params = {'y_target': y, **common_params}

    attack_name = args.attack + attack_name

    print('Running [{}]. Params: {}'.format(args.attack.upper(), attack_params))

    adv_x_op = attack_op.generate(x_op, **attack_params)
    adv_preds_op = tf_model(adv_x_op)
    preds_op = tf_model(x_op)

    n_success = 0
    n_processed = 0
    progress = tqdm(dataloader)
    for paths, x in progress:

        progress.set_description('ATTACK')

        z, adv_x, adv_z = sess.run([preds_op, adv_x_op, adv_preds_op], feed_dict={x_op: x, y: target})

        src, dst = np.argmax(z, axis=1), np.argmax(adv_z, axis=1)
        success = src != dst
        success_paths = np.array(paths)[success]
        success_adv_x = adv_x[success]
        success_src = src[success]
        success_dst = dst[success]

        n_success += success_adv_x.shape[0]
        n_processed += x.shape[0]

        progress.set_postfix({'Success': '{:3.2%}'.format(n_success / n_processed)})
        progress.set_description('SAVING')

        for p, a, s, d in zip(success_paths, success_adv_x, success_src, success_dst):
            path = '{}_{}_src{}_dst{}.npz'.format(p, attack_name, s, d)
            path = os.path.join(args.out_folder, path)
            np.savez_compressed(path, img=a)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Generate adversarial examples')
    parser.add_argument('image_folder', help='Directory containing images to be modified')
    parser.add_argument('out_folder', help='Prefix path for generated adversarial examples')
    parser.add_argument('-b', '--batch-size', type=int, default=1, help='Number of attacks to run simultaneously')
    parser.add_argument('-a', '--attack', choices=('lbfgs', 'fgsm', 'iter', 'm-iter', 'pgd', 'deepfool', 'jsma', 'cw'),
                        default=20,
                        help='Number of attacks to run simultaneously')
    parser.add_argument('-e', '--eps', type=int, default=20, help='Maximum perturbation (in range [0, 255])')
    parser.add_argument('-l', '--ord', type=int, choices=(-1, 1, 2), default=-1,
                        help='Norm order for perturbation (default: inf)')
    args = parser.parse_args()

    args.device = 'cuda' if torch.cuda.is_available() else 'cpu'
    # args.device = 'cpu'
    main(args)
