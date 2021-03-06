from __future__ import print_function, division, absolute_import

import atexit
from datetime import timedelta
import logging
import math
import warnings
import weakref
import toolz

from dask.utils import factors
from tornado import gen

from .cluster import Cluster
from ..compatibility import get_thread_identity
from ..core import CommClosedError
from ..utils import (sync, ignoring, All, silence_logging, LoopRunner,
        log_errors, thread_state, parse_timedelta)
from ..nanny import Nanny
from ..scheduler import Scheduler
from ..worker import Worker, parse_memory_limit, _ncores

logger = logging.getLogger(__name__)


class LocalCluster(Cluster):
    """ Create local Scheduler and Workers

    This creates a "cluster" of a scheduler and workers running on the local
    machine.

    Parameters
    ----------
    n_workers: int
        Number of workers to start
    processes: bool
        Whether to use processes (True) or threads (False).  Defaults to True
    threads_per_worker: int
        Number of threads per each worker
    scheduler_port: int
        Port of the scheduler.  8786 by default, use 0 to choose a random port
    silence_logs: logging level
        Level of logs to print out to stdout.  ``logging.WARN`` by default.
        Use a falsey value like False or None for no change.
    ip: string
        IP address on which the scheduler will listen, defaults to only localhost
    diagnostics_port: int
        Port on which the :doc:`web` will be provided.  8787 by default, use 0
        to choose a random port, ``None`` to disable it, or an
        :samp:`({ip}:{port})` tuple to listen on a different IP address than
        the scheduler.
    asynchronous: bool (False by default)
        Set to True if using this cluster within async/await functions or within
        Tornado gen.coroutines.  This should remain False for normal use.
    kwargs: dict
        Extra worker arguments, will be passed to the Worker constructor.
    service_kwargs: Dict[str, Dict]
        Extra keywords to hand to the running services
    security : Security

    Examples
    --------
    >>> c = LocalCluster()  # Create a local cluster with as many workers as cores  # doctest: +SKIP
    >>> c  # doctest: +SKIP
    LocalCluster("127.0.0.1:8786", workers=8, ncores=8)

    >>> c = Client(c)  # connect to local cluster  # doctest: +SKIP

    Add a new worker to the cluster

    >>> w = c.start_worker(ncores=2)  # doctest: +SKIP

    Shut down the extra worker

    >>> c.stop_worker(w)  # doctest: +SKIP

    Pass extra keyword arguments to Bokeh

    >>> LocalCluster(service_kwargs={'bokeh': {'prefix': '/foo'}})  # doctest: +SKIP
    """
    def __init__(self, n_workers=None, threads_per_worker=None, processes=True,
                 loop=None, start=None, ip=None, scheduler_port=0,
                 silence_logs=logging.WARN, diagnostics_port=8787,
                 services=None, worker_services=None, service_kwargs=None,
                 asynchronous=False, security=None, **worker_kwargs):
        if start is not None:
            msg = ("The start= parameter is deprecated. "
                   "LocalCluster always starts. "
                   "For asynchronous operation use the following: \n\n"
                   "  cluster = yield LocalCluster(asynchronous=True)")
            raise ValueError(msg)

        self.status = None
        self.processes = processes
        self.silence_logs = silence_logs
        self._asynchronous = asynchronous
        self.security = security
        services = services or {}
        worker_services = worker_services or {}
        if silence_logs:
            self._old_logging_level = silence_logging(level=silence_logs)
        if n_workers is None and threads_per_worker is None:
            if processes:
                n_workers, threads_per_worker = nprocesses_nthreads(_ncores)
            else:
                n_workers = 1
                threads_per_worker = _ncores
        if n_workers is None and threads_per_worker is not None:
            n_workers = max(1, _ncores // threads_per_worker)
        if n_workers and threads_per_worker is None:
            # Overcommit threads per worker, rather than undercommit
            threads_per_worker = max(1, int(math.ceil(_ncores / n_workers)))
        if n_workers and 'memory_limit' not in worker_kwargs:
            worker_kwargs['memory_limit'] = parse_memory_limit('auto', 1, n_workers)

        worker_kwargs.update({
            'ncores': threads_per_worker,
            'services': worker_services,
        })

        self._loop_runner = LoopRunner(loop=loop, asynchronous=asynchronous)
        self.loop = self._loop_runner.loop

        if diagnostics_port is not False and diagnostics_port is not None:
            try:
                from distributed.bokeh.scheduler import BokehScheduler
                from distributed.bokeh.worker import BokehWorker
            except ImportError:
                logger.debug("To start diagnostics web server please install Bokeh")
            else:
                services[('bokeh', diagnostics_port)] = (BokehScheduler, (service_kwargs or {}).get('bokeh', {}))
                worker_services[('bokeh', 0)] = BokehWorker

        self.scheduler = Scheduler(loop=self.loop,
                                   services=services,
                                   security=security)
        self.scheduler_port = scheduler_port

        self.workers = []
        self.worker_kwargs = worker_kwargs
        if security:
            self.worker_kwargs['security'] = security

        self.start(ip=ip, n_workers=n_workers)

        clusters_to_close.add(self)

    def __repr__(self):
        return ('LocalCluster(%r, workers=%d, ncores=%d)' %
                (self.scheduler_address, len(self.workers),
                 sum(w.ncores for w in self.workers))
                )

    def __await__(self):
        return self._started.__await__()

    @property
    def asynchronous(self):
        return (
            self._asynchronous or
            getattr(thread_state, 'asynchronous', False) or
            hasattr(self.loop, '_thread_identity') and self.loop._thread_identity == get_thread_identity()
        )

    def sync(self, func, *args, **kwargs):
        if kwargs.pop('asynchronous', None) or self.asynchronous:
            callback_timeout = kwargs.pop('callback_timeout', None)
            future = func(*args, **kwargs)
            if callback_timeout is not None:
                future = gen.with_timeout(timedelta(seconds=callback_timeout),
                                          future)
            return future
        else:
            return sync(self.loop, func, *args, **kwargs)

    def start(self, **kwargs):
        self._loop_runner.start()
        if self._asynchronous:
            self._started = self._start(**kwargs)
        else:
            self.sync(self._start, **kwargs)

    @gen.coroutine
    def _start(self, ip=None, n_workers=0):
        """
        Start all cluster services.
        """
        if self.status == 'running':
            return
        if (ip is None) and (not self.scheduler_port) and (not self.processes):
            # Use inproc transport for optimization
            scheduler_address = 'inproc://'
        elif ip is not None and ip.startswith('tls://'):
            scheduler_address = ('%s:%d' % (ip, self.scheduler_port))
        else:
            if ip is None:
                ip = '127.0.0.1'
            scheduler_address = (ip, self.scheduler_port)
        self.scheduler.start(scheduler_address)

        yield [self._start_worker(**self.worker_kwargs) for i in range(n_workers)]

        self.status = 'running'

        raise gen.Return(self)

    @gen.coroutine
    def _start_worker(self, death_timeout=60, **kwargs):
        if self.status and self.status.startswith('clos'):
            warnings.warn("Tried to start a worker while status=='%s'" % self.status)
            return

        if self.processes:
            W = Nanny
            kwargs['quiet'] = True
        else:
            W = Worker

        w = W(self.scheduler.address, loop=self.loop,
              death_timeout=death_timeout,
              silence_logs=self.silence_logs, **kwargs)
        yield w._start()

        self.workers.append(w)

        while w.status != 'closed' and w.worker_address not in self.scheduler.workers:
            yield gen.sleep(0.01)

        if w.status == 'closed' and self.scheduler.status == 'running':
            self.workers.remove(w)
            raise gen.TimeoutError("Worker failed to start")

        raise gen.Return(w)

    def start_worker(self, **kwargs):
        """ Add a new worker to the running cluster

        Parameters
        ----------
        port: int (optional)
            Port on which to serve the worker, defaults to 0 or random
        ncores: int (optional)
            Number of threads to use.  Defaults to number of logical cores

        Examples
        --------
        >>> c = LocalCluster()  # doctest: +SKIP
        >>> c.start_worker(ncores=2)  # doctest: +SKIP

        Returns
        -------
        The created Worker or Nanny object.  Can be discarded.
        """
        return self.sync(self._start_worker, **kwargs)

    @gen.coroutine
    def _stop_worker(self, w):
        yield w._close()
        if w in self.workers:
            self.workers.remove(w)

    def stop_worker(self, w):
        """ Stop a running worker

        Examples
        --------
        >>> c = LocalCluster()  # doctest: +SKIP
        >>> w = c.start_worker(ncores=2)  # doctest: +SKIP
        >>> c.stop_worker(w)  # doctest: +SKIP
        """
        self.sync(self._stop_worker, w)

    @gen.coroutine
    def _close(self, timeout='2s'):
        # Can be 'closing' as we're called by close() below
        if self.status == 'closed':
            return
        self.status = 'closing'

        self.scheduler.clear_task_state()

        with ignoring(gen.TimeoutError):
            yield gen.with_timeout(
                timedelta(seconds=parse_timedelta(timeout)),
                All([self._stop_worker(w) for w in self.workers]),
            )
        del self.workers[:]

        try:
            with ignoring(gen.TimeoutError, CommClosedError, OSError):
                yield self.scheduler.close(fast=True)
            del self.workers[:]
        finally:
            self.status = 'closed'

    def close(self, timeout=20):
        """ Close the cluster """
        if self.status == 'closed':
            return

        try:
            result = self.sync(self._close, callback_timeout=timeout)
        except RuntimeError:  # IOLoop is closed
            result = None

        if hasattr(self, '_old_logging_level'):
            if self.asynchronous:
                result.add_done_callback(lambda _: silence_logging(self._old_logging_level))
            else:
                silence_logging(self._old_logging_level)

        if not self.asynchronous:
            self._loop_runner.stop()

        return result

    @gen.coroutine
    def scale_up(self, n, **kwargs):
        """ Bring the total count of workers up to ``n``

        This function/coroutine should bring the total number of workers up to
        the number ``n``.

        This can be implemented either as a function or as a Tornado coroutine.
        """
        with log_errors():
            kwargs2 = toolz.merge(self.worker_kwargs, kwargs)
            yield [self._start_worker(**kwargs2)
                   for i in range(n - len(self.scheduler.workers))]

            # clean up any closed worker
            self.workers = [w for w in self.workers if w.status != 'closed']

    @gen.coroutine
    def scale_down(self, workers):
        """ Remove ``workers`` from the cluster

        Given a list of worker addresses this function should remove those
        workers from the cluster.  This may require tracking which jobs are
        associated to which worker address.

        This can be implemented either as a function or as a Tornado coroutine.
        """
        with log_errors():
            # clean up any closed worker
            self.workers = [w for w in self.workers if w.status != 'closed']
            workers = set(workers)

            # we might be given addresses
            if all(isinstance(w, str) for w in workers):
                workers = {w for w in self.workers if w.worker_address in workers}

            # stop the provided workers
            yield [self._stop_worker(w) for w in workers]

    def __del__(self):
        self.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    @gen.coroutine
    def __aenter__(self):
        yield self._started
        raise gen.Return(self)

    @gen.coroutine
    def __aexit__(self, typ, value, traceback):
        yield self._close()

    @property
    def scheduler_address(self):
        try:
            return self.scheduler.address
        except ValueError:
            return '<unstarted>'


def nprocesses_nthreads(n):
    """
    The default breakdown of processes and threads for a given number of cores

    Parameters
    ----------
    n: int
        Number of available cores

    Examples
    --------
    >>> nprocesses_nthreads(4)
    (4, 1)
    >>> nprocesses_nthreads(32)
    (8, 4)

    Returns
    -------
    nprocesses, nthreads
    """
    if n <= 4:
        processes = n
    else:
        processes = min(f for f in factors(n) if f >= math.sqrt(n))
    threads = n // processes
    return (processes, threads)


clusters_to_close = weakref.WeakSet()


@atexit.register
def close_clusters():
    for cluster in list(clusters_to_close):
        cluster.close(timeout=10)
