import numpy as np
import torch as t
import time
import hgdl.misc as misc
from hgdl.local_methods.local_optimizer import run_local_optimizer
from hgdl.global_methods.global_optimizer import run_global
from hgdl.local_methods.local_optimizer import run_local
import dask.distributed as distributed
import dask.multiprocessing
from dask.distributed import as_completed
from hgdl.result.optima  import optima
###authors: David Perryman, Marcus Noack
###institution: CAMERA @ Lawrence Berkeley National Laboratory
import pickle


"""
TODO:   *the radius is still ad hoc, should be related to curvature and unique to deflation point
"""


class HGDL:
    """
    This is the HGDL class, a class to do asynchronous HPC-driven optimization
    type help(name you gave at import)
    e.g.:
    from hgdl.hgdl import HGDL
    help(HGDL)
    """
    def __init__(self,obj_func,grad_func,hess_func,
            bounds, maxEpochs=100000,
            local_optimizer = "newton",
            global_optimizer = "genetic",
            radius = 0.1, global_tol = 1e-4,
            local_max_iter = 20, global_max_iter = 0,
            number_of_walkers = 20,
            args = (), verbose = False):
        """
        intialization for the HGDL class

        required input:
        ---------------
            obj_func
            grad_func
            hess_func
            bounds
        optional input:
        ---------------
            maxEpochs = 100000
            local_optimizer = "newton"   "newton"/user defined function, 
                                         use partial to communicate args to the function
            global_optimizer = "genetic"   "genetic"/"gauss"/user defined function,
                                           use partial to communicate args to the function
            radius = 20
            global_tol = 1e-4
            local_max_iter = 20
            global_max_iter = 20
            number_of_walkers: make sure you have enough workers for
                               your walkers ( walkers + 1 <= workers)
            args = (), a n-tuple of parameters, will be communicated to obj_func, grad, hess
            verbose = False
        """
        self.obj_func = obj_func
        self.grad_func= grad_func
        self.hess_func= hess_func
        self.bounds = np.asarray(bounds)
        self.r = radius
        self.dim = len(self.bounds)
        self.global_tol = global_tol
        self.local_max_iter = local_max_iter
        self.global_max_iter = global_max_iter
        self.number_of_walkers = number_of_walkers
        self.maxEpochs = maxEpochs
        self.local_optimizer = local_optimizer
        self.global_optimizer = global_optimizer
        self.args = args
        self.verbose = verbose
        self.optima = optima(self.dim)
    ###########################################################################
    ###########################################################################
    def optimize(self, dask_client = True, x0 = None):
        """
        optional input:
        -----
            dask_client = True = dask.distributed.Client()
            x0 = None = randon.rand()   starting positions
        """
        ######initialize starting positions#######
        if dask_client is None: raise Exception("dask_client is None, can only be True/False or a distributed.Client(...)")
        self._prepare_starting_positions(x0)
        #####initializedask client###############
        client = self._prepare_dask_client(dask_client)
        worker_info = client.scheduler_info()["workers"]
        self.me = list(worker_info.keys())[0]
        print("global me: ",self.me)
        #####run first local######################
        x,f,grad_norm,eig,success = self._run_first_local_optimization(client)
        ##########################################
        print("HGDL engine started: ")
        #print(self.x0)
        print("")
        print("I found ",len(np.where(success == True)[0])," optima in my first run")
        if len(np.where(success == True)[0]) == 0: 
            print("no optima found in first round")
            success[:] = True
        else:
            self.optima.list["success"] = True
        print("Points stored in the optima list")
        self.optima.fill_in_optima_list(x,f,grad_norm,eig, success)
        if self.verbose == True: print(optima_list)
        #################################
        ####run epochs###################
        #################################
        self._run_epochs(client)
    ###########################################################################
    def get_latest(self, n):
        """
        get n best results

        input:
        -----
            n: number of results requested
        """
        try:
            data, frames = self.transfer_data.get()
            self.optima = distributed.protocol.deserialize(data,frames)
        except:
            self.optima = self.optima
        optima_list = self.optima.list
        n = min(n,len(optima_list["x"]))
        return {"x": optima_list["x"][0:n], \
                "func evals": optima_list["func evals"][0:n],
                "classifier": optima_list["classifier"][0:n],
                "eigen values": optima_list["eigen values"][0:n],
                "gradient norm":optima_list["gradient norm"][0:n],
                "success":optima_list["success"]}
    ###########################################################################
    def get_final(self,n):
        """
        get n final results

        input:
        -----
            n: number of results requested
        """
        try:
            self.optima = self.main_future.result()
        except:
            pass
        optima_list = self.optima.list
        n = min(n,len(optima_list["x"]))
        return {"x": optima_list["x"][0:n], \
                "func evals": optima_list["func evals"][0:n],
                "classifier": optima_list["classifier"][0:n],
                "eigen values": optima_list["eigen values"][0:n],
                "gradient norm":optima_list["gradient norm"][0:n],
                "success":optima_list["success"]}
    ###########################################################################
    def cancel_tasks(self):
        """
        cancel tasks but leave client alive
        return:
        -------
            latest results
        """
        res = self.get_latest(-1)
        self.break_condition.set(True)
        while self.main_future.status != "finished":
            time.sleep(0.1)
        self.client.cancel(self.main_future)
        print("Status of HGDL task: ", self.main_future.status)
        print("This leaves the client alive.")
        return res
    ###########################################################################
    def kill(self, n= -1):
        """
        kill tasks and shutdown client
        return:
        -------
            latest results
        """
        print("Kill initialized ...")
        res = self.get_latest(n)
        try:
            self.break_condition.set(True)
            while self.main_future.status == "pending":
                time.sleep(0.1)
            self.client.gather(self.main_future)
            self.client.cancel(self.main_future)
            del self.main_future
            self.client.shutdown()
            self.client.close()
            print("kill successful")
        except Exception as err:
            print(err)
            print("kill failed")
        time.sleep(0.1)
        return res
    ###########################################################################
    def _prepare_starting_positions(self,x0):
        if x0 is None: self.x0 = misc.random_population(self.bounds,self.number_of_walkers)
        elif len(x0) < self.number_of_walkers: 
            self.x0 = np.empty((self.number_of_walkers,len(x0[0])))
            self.x0[0:len(x0)] = x0
            self.x0[len(x0):] = misc.random_population(self.bounds,self.number_of_walkers - len(x0))
        elif len(x0) > self.number_of_walkers:
            self.x0 = x0[0:self.number_of_walkers]
        else: self.x0 = x0
    ###########################################################################
    def _prepare_dask_client(self,dask_client):
        if dask_client is True: dask_client = dask.distributed.Client()
        client = dask_client
        return client
    ###########################################################################
    def _run_first_local_optimization(self,client):
        if client is False:
            x,f,grad_norm,eig,success = run_local_optimizer(self.obj_func,
                self.grad_func,self.hess_func,
                self.bounds,self.r,self.local_max_iter,self.local_optimizer,
                self.x0[0:4],self.args)
        else:
            d = {"func": self.obj_func,
                 "grad": self.grad_func,"hess":self.hess_func,
                 "bounds":self.bounds, "radius":self.r,"local max iter":self.local_max_iter,
                 "local optimizer":self.local_optimizer,
                 "x0":self.x0[0:4],"args":self.args}
            bf = client.scatter(d,workers = self.me)
            self.main_future = client.submit(run_local_optimizer,bf, workers = self.me)
            x,f,grad_norm,eig,success = self.main_future.result()
        return x,f,grad_norm,eig,success
    ###########################################################################
    def _run_epochs(self,client):
        dask_client = client
        if self.verbose == True: print("Submitting main hgdl task")
        if dask_client is False:
            self.transfer_data = False
            self.break_condition = False
            hgdl(self.transfer_data,self.break_condition,
                self.optima,self.obj_func,
                self.grad_func,self.hess_func,
                self.bounds,self.maxEpochs,self.r,self.local_max_iter,
                self.global_max_iter,self.local_optimizer,self.global_optimizer,
                self.number_of_walkers,self.args, self.verbose)
        else:
            self.break_condition = distributed.Variable("break_condition",client)
            self.transfer_data = distributed.Variable("transfer_data",client)
            a = distributed.protocol.serialize(self.optima)
            self.transfer_data.set(a)
            self.break_condition.set(False)
            print("starting")
            d = {"transfer data":self.transfer_data,
                 "break condition":self.break_condition,
                 "optima":self.optima, "func":self.obj_func,
                 "grad":self.grad_func,"hess":self.hess_func,
                 "bounds":self.bounds,"max epochs":self.maxEpochs,
                 "radius":self.r,"local max iter":self.local_max_iter,
                 "global max iter":self.global_max_iter,
                 "local optimizer":self.local_optimizer,
                 "global optimizer":self.global_optimizer,
                 "number of walkers":self.number_of_walkers,"args":self.args, 
                 "verbose":self.verbose}
            bf = client.scatter(d,workers = self.me)
            self.main_future = client.submit(hgdl, bf,workers = self.me)
            self.client = client
###########################################################################
###########################################################################
##################hgdl functions###########################################
###########################################################################
###########################################################################
def hgdl(d):
    if d["verbose"] is True: print("    Starting ",maxEpochs," epochs.")
    for i in range(d["max epochs"]):
        if d["break condition"] is not False: bc = d["break condition"].get()
        else: bc = False
        if bc is True: print("Epoch ",i," was cancelled");break
        print("Computing epoch ",i," of ",d["max epochs"])
        optima = run_hgdl_epoch(d)
        if d["transfer data"] is not False:
            a = distributed.protocol.serialize(optima)
            d["transfer data"].set(a)
        if d["verbose"] is True: print("    Epoch ",i," finished")
    return optima
###########################################################################
def run_hgdl_epoch(d):
    """
    an epoch is one local run and one global run,
    where one local run are several convergence runs of all workers from
    the x_init point
    """
    optima_list  = d["optima"].list
    n = len(optima_list["x"])
    nn = min(n,d["number of walkers"])
    if d["verbose"] is True: print("    global step started")
    x0 = run_global(\
            np.array(optima_list["x"][0:nn,:]),
            np.array(optima_list["func evals"][0:nn]),
            d["bounds"], d["global optimizer"],d["number of walkers"],d["verbose"])
    #if verbose is True: print("    global step finished")
    #if verbose is True: print("    local step started")
    d["x0"] = x0
    optima = run_local(d)
    optima.list["success"] = True
    #if verbose is True: print("    local step finished")
    return optima


