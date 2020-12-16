import numpy as np
from dask.distributed import get_client

class Results(object):
    def __init__(self, info):
        self.func = info.func
        self.bestX = info.bestX
        self.N = info.num_individuals
        self.scheduler_address = info.scheduler_address 
        self.minima_x = np.empty((0, info.k), np.float64)
        self.minima_y = np.empty(0, np.float64)
        self.global_x = np.empty((0, info.k), np.float64)
        self.global_y = np.empty(0, np.float64)
    def update_minima(self, new_minima):
        client = get_client(address=self.scheduler_address) 
        minima_y = np.array([f.result() for f in client.map(self.func, new_minima)])
        self.minima_x = np.append(self.minima_x, new_minima, 0)
        self.minima_y = np.append(self.minima_y, minima_y)
    def update_global(self, new_global):
        client = get_client(address=self.scheduler_address) 
        global_y = np.array([f.result() for f in client.map(self.func, new_global)])
        self.global_x = np.append(self.global_x, new_global, 0)
        self.global_y = np.append(self.global_y, global_y)
    def get_all(self):
        x = np.append(self.minima_x, self.global_x, 0)
        y = np.append(self.minima_y, self.global_y)
        return x, y
    def sort(self):
        x = np.append(self.minima_x, self.global_x, 0)
        y = np.append(self.minima_y, self.global_y)
        c = np.argmin(y)
        return x[c], y[c]
    def epoch_end(self):
        x, y = self.sort()
        res = {"best_x":x,
                "best_y":y}
        return res

    def latest(self, N):
        x, y = self.sort()
        c1, c2 = np.argsort(self.minima_y), np.argsort(self.global_y)
        self.minima_x, self.minima_y = self.minima_x[c1], self.minima_y[c1]
        self.global_x, self.global_y = self.global_x[c2], self.global_y[c2]
        return {
            'best_x':x,'best_y':y,
            'minima_x':self.minima_x[:N],
            'minima_y':self.minima_y[:N],
            'global_x':self.global_x[:N],
            'global_y':self.global_y[:N]
        }

    def roll_up(self):
        x, y = self.sort()
        c1, c2 = np.argsort(self.minima_y), np.argsort(self.global_y)
        self.minima_x, self.minima_y = self.minima_x[c1], self.minima_y[c1]
        self.global_x, self.global_y = self.global_x[c2], self.global_y[c2]
        return {
            'best_x':x,'best_y':y,
            'minima_x':self.minima_x[:self.bestX],
            'minima_y':self.minima_y[:self.bestX],
            'global_x':self.global_x[:self.bestX],
            'global_y':self.global_y[:self.bestX]
        }
