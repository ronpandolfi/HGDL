import numpy as np
import hgdl.misc as misc
import hgdl.deflation.bump_function as defl
import dask.distributed as distributed

def DNewton(data):
    e = np.inf
    success = True
    counter = 0
    tol = 1e-6
    func = data["func"]
    grad = data["grad"]
    hess = data["hess"]
    x0 = data["x0"]
    x_defl = data["x_defl"]
    bounds = data["bounds"]
    radius = data["radius"]
    max_iter = data["local max iter"]
    args = data["args"]
    x = np.array(x0)

    #me = distributed.get_worker()
    #print("                me and my data: ",me,x0)
    while e > tol:
        counter += 1
        d = defl.deflation_function(x,x_defl,radius)
        dg = defl.deflation_function_gradient(x,x_defl,radius)
        gradient = grad(x, *args)
        hessian = hess(x, *args)
        e = np.linalg.norm(d*gradient)
        try:
            gamma = np.linalg.solve(hessian + (np.outer(gradient,dg)/d),-gradient)
        except Exception as error: 
            gamma,a,b,c = np.linalg.lstsq(hessian + (np.outer(gradient,dg)/d),-gradient)
        x += gamma
        if counter >= max_iter or misc.out_of_bounds(x,bounds):
            x,f, s = gradient_descent(func,grad,bounds,x_defl,x-gamma,radius,args)
            return x,f,e,np.linalg.eig(hess(x, *args))[0],s
            #return x0,func(x0, *args),e,np.linalg.eig(hess(x0, *args))[0],False
    data["result"] = (x,func(x, *args),e,np.linalg.eig(hessian)[0], success)
    return x,func(x, *args),e,np.linalg.eig(hessian)[0], success



def gradient_descent(ObjectiveFunction, GradientFunction,bounds,x_defl,x0, radius, args):
    dim = len(bounds)
    bounds = np.array(bounds)
    epsilon = np.inf
    step_counter = 0
    beta = 0.5
    x = np.array(x0)
    success = True
    while epsilon > 1e-6:
        step_counter += 1
        d = defl.deflation_function(x,x_defl,radius)
        gradient = GradientFunction(x, *args) * d
        counter = 0
        step = 1.0
        while misc.out_of_bounds(x - (step * gradient), bounds) or \
              ObjectiveFunction(x - (step * gradient), *args) > \
              ObjectiveFunction(x, *args) - ((step / 2.0) * np.linalg.norm(gradient) ** 2):
            step = step * beta
            counter += 1
            if counter > 10:
                return x, ObjectiveFunction(x, *args), False
        x = x - (step * gradient)
        epsilon = np.linalg.norm(gradient)
        if step_counter > 20:
            success = False
            break
    return x, ObjectiveFunction(x, *args), True

