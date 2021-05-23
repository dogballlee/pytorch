import torch
import torch.nn as nn
from torch.nn.parallel import DistributedDataParallel as DDP

from .DdpTrainerBase import DdpTrainerBase


class DdpTrainer(DdpTrainerBase):

    class HookState:

        def __init__(self, cref, process_group):
            r"""
            holds state information that is needed by the communication hook
            during the training algorithm.
            Args:
                cref (object): reference to the self keyword of the trainer instance
                process_group (object): distributed process group
            """
            self.cref = cref
            self.process_group = process_group
            self.batch_number = -1

        def get_key(self, bucket_index):
            r"""
            returns an encoded key that represents the current batch and
            bucket index.
            Args:
                bucket_index (int): index of the bucket being processed in backward
            """
            return f"{self.batch_number},{bucket_index}"

        def next_batch(self):
            r"""
            increments batch_number by 1
            """
            self.batch_number += 1

    def __init__(self, rank, trainer_count, process_group, use_cuda_rpc, server_rref, backend, epochs):
        r"""
        a trainer that implements ddp using a simple hook that performs allreduce
        using the process group.
        Args:
            rank (int): worker rank
            trainer_count (int): count of trainer in the world
            process_group (object): distributed process group
            use_cuda_rpc (bool): indicator to determine if this is a CUDA metric
            server_rref (object): remote reference to the server
            backend (string): distributed communication backend
            epochs (int): epoch count for training
        """
        super().__init__(rank)
        self.rank = rank
        self.trainer_count = trainer_count
        self.process_group = process_group
        self.use_cuda_rpc = use_cuda_rpc
        self.server_rref = server_rref
        self.backend = backend
        self.epochs = epochs

    @staticmethod
    def hook(state, bucket):
        r"""
        ddp communication hook that uses the current backend allreduce
        implementation.
        Args:
            state (object): maintains state during the training process
            bucket (object): gradient bucket
        """
        cref = state.cref
        tensors = [bucket.get_tensor() / state.process_group.size()]
        key = state.get_key(bucket.get_index())
        cref.record_hook_fut_start(key, f"{cref.backend}_allreduce")
        fut = state.process_group.allreduce(tensors).get_future()

        def callback(fut):
            cref.record_hook_fut_end(key)
            return fut.wait()

        return fut.then(callback)

    def get_hook(self):
        r"""
        returns DdpTrainer.hook
        """
        return DdpTrainer.hook

    def create_ddp_model(self, model):
        r"""
        creates a ddp_model and hook_state objects, registers ddp_model communication hook,
        and returns the ddp_model and hook_state.
        Args:
            model (object): neural network model
        """
        ddp_model = DDP(
            model, device_ids=[self.rank], process_group=self.process_group
        )
        hook_state = self.HookState(self, self.process_group)
        ddp_model.register_comm_hook(hook_state, self.get_hook())
        return ddp_model, hook_state

    def epoch_key(self, epoch, index):
        r"""
        returns an encoded key that represents the current epoch and
        iteration index.
        Args:
            epoch (int): epoch index
            index (int): iteration index
        """
        return f"{epoch},{index}"

    def preprocess_data(self, data):
        r"""
        moves the data from CPU to GPU.
        Args:
            data (list): training examples
        """
        for i in range(len(data)):
            data[i][0] = data[i][0].cuda(self.rank)
            data[i][1] = data[i][1].cuda(self.rank)
        return data

    def iteration_step(self, ddp_model, criterion, optimizer, hook_state, epoch, index, batch):
        r"""
        performs an iteration of training.
        Args:
            ddp_model (object): distributed data parallel model
            criterion (object): loss function to measure model
            optimizer (object): updates model parameters
            hook_state (object): ddp communication hook state object
            epoch (int): index of pass through the data
            index (int): iteration number - 1 in current batch
            batch (list): training examples
        """
        hook_state.next_batch()
        input, target = batch[0], batch[1]
        self.record_batch_start(self.epoch_key(epoch, index))
        optimizer.zero_grad()
        self.record_forward_start(self.epoch_key(epoch, index))
        out = ddp_model(input)
        self.record_forward_end(self.epoch_key(epoch, index))
        loss = criterion(out, target)
        self.record_backward_start(self.epoch_key(epoch, index))
        loss.backward()
        self.record_backward_end(self.epoch_key(epoch, index))
        optimizer.step()
        self.record_batch_end(self.epoch_key(epoch, index))

    def train(self, model, data):
        r"""
        implements the training algorithm for the current trainer.
        Args:
            model (object): neural network model
            data (list): training examples
        """
        model = model.cuda(self.rank)
        data = self.preprocess_data(data)
        ddp_model, hook_state = self.create_ddp_model(model)
        criterion = nn.CrossEntropyLoss().cuda(self.rank)
        optimizer = torch.optim.SGD(ddp_model.parameters(), 1e-4)

        for epoch in range(self.epochs):
            for index, batch in enumerate(data):
                self.iteration_step(
                    ddp_model, criterion, optimizer, hook_state, epoch, index, batch
                )
        torch.cuda.synchronize(self.rank)
