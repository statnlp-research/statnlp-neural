from statnlp.hypergraph import NetworkCompiler
from statnlp.hypergraph import NetworkIDMapper
from statnlp.hypergraph import TensorBaseNetwork
from statnlp.hypergraph import TensorGlobalNetworkParam
from statnlp.hypergraph import NeuralBuilder
from statnlp.hypergraph import NetworkModel
from statnlp.hypergraph import NetworkConfig
import torch
import torch.nn as nn
import numpy as np
from statnlp.hypergraph.Utils import load_emb_glove, load_emb_word2vec
from statnlp.common import BaseInstance
from statnlp.common.eval import label_eval
from termcolor import colored
import torch.nn.functional as F
import random
import gensim
import argparse


class LRNetworkCompiler(NetworkCompiler):

    def __init__(self, label_map):
        super().__init__()
        self.labels = ["x"] * len(label_map)
        self.label2id = label_map
        # print(self.labels)
        for key in self.label2id:
            self.labels[self.label2id[key]] = key

        self.label_size = len(self.labels)
        # print("Inside compiler: ", self.labels)
        NetworkIDMapper.set_capacity(np.asarray([0, 10], dtype=np.int64))

        # print(self.label2id)
        # print(self.labels)
        self._all_nodes = None
        self._all_children = None

        print("Building generic network...")
        self.build_generic_network()

    def to_root(self):
        return self.to_node(2, 0)

    def to_tag(self, label_id):
        return self.to_node(1, label_id)

    def to_leaf(self):
        return self.to_node(0, 0)

    def to_node(self, pos, label_id):
        return NetworkIDMapper.to_hybrid_node_ID(np.asarray([pos, label_id]))

    def compile_labeled(self, network_id, inst, param):

        builder = TensorBaseNetwork.NetworkBuilder.builder()
        leaf = self.to_leaf()
        builder.add_node(leaf)
        output = inst.get_output()
        children = [leaf]
        label = output
        tag_node = self.to_tag(self.label2id[label])
        builder.add_node(tag_node)
        builder.add_edge(tag_node, children)
        children = [tag_node]
        root = self.to_root()
        builder.add_node(root)
        builder.add_edge(root, children)
        network = builder.build(network_id, inst, param, self)
        return network

    def compile_unlabeled(self, network_id, inst, param):
        builder = TensorBaseNetwork.NetworkBuilder.builder()
        root_node = self.to_root()
        all_nodes = self._all_nodes
        root_idx = np.argwhere(all_nodes == root_node)[0][0]
        node_count = root_idx + 1
        network = builder.build_from_generic(network_id, inst, self._all_nodes, self._all_children, node_count,
                                             self.num_hyperedge, param, self)
        return network

    def build_generic_network(self):
        builder = TensorBaseNetwork.NetworkBuilder.builder()
        leaf = self.to_leaf()
        builder.add_node(leaf)
        root = self.to_root()
        builder.add_node(root)
        children = [leaf]
        for l in range(self.label_size):
            tag_node = self.to_tag(l)
            builder.add_node(tag_node)
            for child in children:
                builder.add_edge(tag_node, [child])
            builder.add_edge(root, [tag_node])
        self._all_nodes, self._all_children, self.num_hyperedge = builder.pre_build()

    def decompile(self, network):
        inst = network.get_instance()
        root_node = self.to_root()
        all_nodes = network.get_all_nodes()
        curr_idx = np.argwhere(all_nodes == root_node)[0][
            0]  # network.count_nodes() - 1 #self._all_nodes.index(root_node)
        children = network.get_max_path(curr_idx)
        child = children[0]
        child_arr = network.get_node_array(child)
        prediction = self.labels[child_arr[1]]
        inst.set_prediction(prediction)
        return inst


class LRNeuralBuilder(NeuralBuilder):
    def __init__(self, gnp, voc_size, label_size, dropout=0.5):
        super().__init__(gnp)
        self.token_embed = 300
        self.label_size = label_size
        print("vocab size: ", voc_size)
        # self.word_embed = nn.Embedding(voc_size, self.token_embed, padding_idx=0).to(NetworkConfig.DEVICE)
        self.word_embed = nn.Embedding(voc_size, self.token_embed).to(NetworkConfig.DEVICE)

        self.input_channel = 1
        self.num_filters = 100
        self.windows = [3, 4, 5]
        self.convolutions = nn.ModuleList(
            [nn.Conv2d(self.input_channel, self.num_filters, (K, self.token_embed)).to(NetworkConfig.DEVICE) for K in
             self.windows])
        """K: 3,4,5
        """
        self.dropout = nn.Dropout(dropout).to(NetworkConfig.DEVICE)

        self.linear = nn.Linear(self.num_filters * len(self.windows), label_size).to(NetworkConfig.DEVICE)

    def load_google_pretrain(self, path, word2idx):
        print("Loading google binary word2vec model")
        model = gensim.models.KeyedVectors.load_word2vec_format(path, binary=True)
        emb = load_emb_word2vec(model, word2idx)
        self.word_embed.weight.data.copy_(torch.from_numpy(emb))
        self.word_embed = self.word_embed.to(NetworkConfig.DEVICE)

    def load_pretrain(self, path, word2idx):
        emb = load_emb_glove(path, word2idx, self.token_embed)
        self.word_embed.weight.data.copy_(torch.from_numpy(emb))
        self.word_embed = self.word_embed.to(NetworkConfig.DEVICE)

    # @abstractmethod
    # def extract_helper(self, network, parent_k, children_k, children_k_index):
    #     pass
    def build_nn_graph(self, instance):
        # print(instance.input)
        word_vec = self.word_embed(instance.word_seq).unsqueeze(0).unsqueeze(
            0)  ##batch=1 x in_channel=1 x sent_len x embedding size
        word_rep = self.dropout(word_vec)
        x = [F.relu(conv(word_rep)).squeeze(3) for conv in
             self.convolutions]  # [(1, hidden_size, sent_len -2), ...]*len(Ks)
        x = [F.max_pool1d(i, i.size(2)).squeeze(2) for i in x]  # [(1, hidden_size), ...]*len(Ks)
        x = torch.cat(x, 1)
        x = self.dropout(x)  # (1, 3*hidden_size)
        logit = self.linear(x).squeeze(0)  # (num label)
        zero_tensor = torch.zeros(1).to(NetworkConfig.DEVICE)
        return torch.cat([logit, zero_tensor], 0)

    def get_nn_score(self, network, parent_k):
        parent_arr = network.get_node_array(parent_k)  # pos, label_id, node_type
        pos = parent_arr[0]
        label_id = parent_arr[1]

        if pos == 0 or pos == 2:  # Start, End
            return torch.tensor(0.0).to(NetworkConfig.DEVICE)
        else:
            nn_output = network.nn_output
            return nn_output[label_id]

    def get_label_id(self, network, parent_k):
        parent_arr = network.get_node_array(parent_k)
        return parent_arr[1]

    def build_node2nn_output(self, network):
        size = network.count_nodes()
        nodeid2nn = [0] * size
        for k in range(size):
            parent_arr = network.get_node_array(k)  # pos, label_id, node_type
            pos = parent_arr[0]
            label_id = parent_arr[1]
            if pos == 0 or pos == 2:  # Start, End
                idx = self.label_size
            else:
                idx = label_id
            nodeid2nn[k] = idx
        return nodeid2nn


class LRReader():
    label2id_map = {}

    @staticmethod
    def read_insts(file, is_labeled, number):
        insts = []
        f = open(file, 'r', encoding='utf-8')
        for line in f:
            line = line.strip()

            fields = line.split()
            label = fields[0]

            words = [word for word in fields[1:]]
            inst = BaseInstance(len(insts) + 1, 1, words, label)
            if is_labeled:
                inst.set_labeled()
            else:
                inst.set_unlabeled()
            insts.append(inst)
            if not label in LRReader.label2id_map:
                output_id = len(LRReader.label2id_map)
                LRReader.label2id_map[label] = output_id

            if len(insts) >= number and number > 0:
                break
        f.close()
        return insts


UNK = "<UNK>"
PAD = "<PAD>"


def parse_arguments(parser):
    ###Training Hyperparameters
    parser.add_argument('--dataset', type=str, default='subj')
    parser.add_argument('--device', type=str, default="cpu")
    parser.add_argument('--num_iter', type=int, default=10)
    parser.add_argument('--emb', type=str, default='glove')
    args = parser.parse_args()
    return args


if __name__ == "__main__":

    NetworkConfig.BUILD_GRAPH_WITH_FULL_BATCH = True
    NetworkConfig.IGNORE_TRANSITION = True
    # NetworkConfig.ECHO_TRAINING_PROGRESS = -1
    # NetworkConfig.LOSS_TYPE = LossType.SSVM
    NetworkConfig.NEUTRAL_BUILDER_ENABLE_NODE_TO_NN_OUTPUT_MAPPING = True
    torch.manual_seed(42)
    torch.set_num_threads(40)
    np.random.seed(42)
    random.seed(42)

    parser = argparse.ArgumentParser(description="Maximum Entropy Model")
    args = parse_arguments(parser)

    data_file = "data/classification/" + args.dataset + ".txt"

    num_train = -1
    num_dev = -1
    num_test = -1
    num_iter = args.num_iter
    batch_size = 1
    num_thread = 1
    emb_path = args.emb
    load_emb = False
    # dev_file = test_file

    NetworkConfig.DEVICE = torch.device(args.device)

    if num_thread > 1:
        NetworkConfig.NUM_THREADS = num_thread
        print('Set NUM_THREADS = ', num_thread)

    all_insts = LRReader.read_insts(data_file, True, num_train)
    vocab2id = {}
    vocab2id[PAD] = 0
    for inst in all_insts:
        for word in inst.input:
            if word not in vocab2id:
                vocab2id[word] = len(vocab2id)
    print("map:", LRReader.label2id_map)
    print(colored('vocab_2id:', 'red'), len(vocab2id))
    print(list(LRReader.label2id_map.keys()))

    for inst in all_insts:
        seq = [vocab2id[word] for word in inst.input] + [0] * (5 - len(inst.input))
        inst.word_seq = torch.tensor(seq).to(NetworkConfig.DEVICE)

    num_folds = 10
    fold_size = len(all_insts) // num_folds
    all_acc = 0
    for k in range(num_folds):
        print("Running fold: {}".format(k + 1))
        end = (k + 1) * fold_size if k != num_folds - 1 else len(all_insts)
        test_insts = all_insts[k * fold_size: end]
        train_insts = all_insts[0:k * fold_size] + all_insts[end: len(all_insts)]
        assert len(train_insts) + len(test_insts) == len(all_insts)
        for inst in train_insts:
            inst.set_labeled()
        for inst in test_insts:
            inst.set_unlabeled()

        gnp = TensorGlobalNetworkParam()
        fm = LRNeuralBuilder(gnp, len(vocab2id), len(LRReader.label2id_map))
        # fm.load_pretrain('data/glove.6B.100d.txt', vocab2id)
        if load_emb:
            fm.load_google_pretrain('data/GoogleNews-vectors-negative300.bin', vocab2id)
        # fm.load_pretrain(None, vocab2id)

        compiler = LRNetworkCompiler(LRReader.label2id_map)
        evaluator = label_eval()
        model = NetworkModel(fm, compiler, evaluator)
        model.check_every = 2000
        if batch_size == 1:
            model.learn(train_insts, num_iter, test_insts, test_insts)
        else:
            model.learn_batch(train_insts, num_iter, test_insts, batch_size)
        model.load_state_dict(torch.load('best_model.pt'))
        results = model.test(test_insts)
        # for inst in results:
        #     print(inst.get_input())
        #     print(inst.get_output())
        #     print(inst.get_prediction())
        #     print()

        ret = model.evaluator.eval(test_insts)
        all_acc += ret.fscore
        print(ret)
    print("Average accuracy: {}".format(all_acc / num_folds))
