import numpy as np
import hgdl.misc as misc
import hgdl.local_methods.bump_function as defl
import dask.distributed as distributed


def DNewton(func,grad,hess,bounds,x0,max_iter,tol,*args):
    e = np.inf
    gradient = np.ones((len(x0))) * np.inf
    counter = 0
    x = np.array(x0)
    grad_list = []
    while e > tol or np.max(abs(gradient)) > tol:
        gradient = grad(x,*args)
        if hess: hessian  = hess(x,*args)
        else: hessian = approximate_hessian(x,grad,*args)
        grad_list.append(np.max(gradient))
        try: gamma = np.linalg.solve(hessian,-gradient)
        except Exception as error: gamma,a,b,c = np.linalg.lstsq(hessian,-gradient)
        x += gamma
        e = np.max(abs(gamma))
        if counter > max_iter: return x,func(x, *args),gradient,np.linalg.eig(hess(x, *args))[0], False
        counter += 1
    return x,func(x, *args),gradient,np.linalg.eig(hess(x, *args))[0], True



def approximate_hessian(x, grad, *args):
    ##implements a first-order approximation
    len_x = len(x)
    hess = np.zeros((len_x,len_x))
    epsilon = 1e-6
    grad_x = grad(x, *args)
    for i in range(len_x):
        x_temp = np.array(x)
        x_temp[i] = x_temp[i] + epsilon
        hess[i,i:] = ((grad(x,*args) - grad_x)/epsilon)[i:]
    return hess + hess.T - np.diag(np.diag(hess))







