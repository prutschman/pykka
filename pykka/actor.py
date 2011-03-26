import logging
import sys
import threading
import uuid

try:
    # Python 2.x
    import Queue as queue
except ImportError:
    # Python 3.x
    import queue  # pylint: disable = F0401

from pykka import ActorDeadError
from pykka.future import ThreadingFuture
from pykka.proxy import ActorProxy
from pykka.registry import ActorRegistry

logger = logging.getLogger('pykka')


class Actor(object):
    """
    To create an actor:

    1. subclass one of the :class:`Actor` implementations, e.g.
       :class:`pykka.gevent.GeventActor` or :class:`ThreadingActor`,
    2. implement your methods, including :meth:`__init__`, as usual,
    3. call :meth:`Actor.start` on your actor class, passing the method any
       arguments for your constructor.

    To stop an actor, call :meth:`Actor.stop()` or :meth:`ActorRef.stop()`.

    For example::

        from pykka.actor import ThreadingActor

        class MyActor(ThreadingActor):
            def __init__(self, my_arg=None):
                ... # My optional init code with access to start() arguments

            def pre_start(self):
                ... # My optional setup code in same context as react()

            def post_stop(self):
                ... # My optional cleanup code in same context as react()

            def react(self, message):
                ... # My optional react code for a plain actor

            def a_method(self, ...):
                ... # My regular method to be used through an ActorProxy

        my_actor_ref = MyActor.start(my_arg=...)
        my_actor_ref.stop()
    """

    @classmethod
    def start(cls, *args, **kwargs):
        """
        Start an actor and register it in the
        :class:`pykka.registry.ActorRegistry`.

        Any arguments passed to :meth:`start` will be passed on to the class
        constructor.

        Returns a :class:`ActorRef` which can be used to access the actor in a
        safe manner.

        Behind the scenes, the following is happening when you call
        :meth:`start`::

            Actor.start()
                Actor.__new__()
                    superclass.__new__()
                    superclass.__init__()
                    UUID assignment
                    Inbox creation
                    ActorRef creation
                Actor.__init__()        # Your code can run here
                superclass.start()
                ActorRegistry.register()
        """
        obj = cls(*args, **kwargs)
        cls._superclass.start(obj)
        logger.debug('Started %s', obj)
        ActorRegistry.register(obj.actor_ref)
        return obj.actor_ref

    #: The actor URN string is a universally unique identifier for the actor.
    #: It may be used for looking up a specific actor using
    #: :meth:`pykka.registry.ActorRegistry.get_by_urn`.
    actor_urn = None

    #: The actors inbox. Use :meth:`ActorRef.send_one_way` and friends to put
    #: messages in the inbox.
    actor_inbox = None

    #: The actor's :class:`ActorRef` instance.
    actor_ref = None

    #: Wether or not the actor should continue processing messages. Use
    #: :meth:`stop` to change it.
    actor_runnable = True

    def __new__(cls, *args, **kwargs):
        obj = cls._superclass.__new__(cls)
        cls._superclass.__init__(obj)
        obj.actor_urn = uuid.uuid4().urn
        # pylint: disable = W0212
        obj.actor_inbox = obj._new_actor_inbox()
        # pylint: enable = W0212
        obj.actor_ref = ActorRef(obj)
        return obj

    # pylint: disable = W0231
    def __init__(self):
        """
        Your are free to override :meth:`__init__` and do any setup you need to
        do. You should not call ``super(YourClass, self).__init__(...)``, as
        that has already been done when your constructor is called.

        When :meth:`__init__` is called, the internal fields
        :attr:`actor_urn`, :attr:`actor_inbox`, and :attr:`actor_ref` are
        already set, but the actor is not started or registered in
        :class:`pykka.registry.ActorRegistry`.
        """
        pass
    # pylint: enable = W0231

    def __str__(self):
        return '%(class)s (%(urn)s)' % {
            'class': self.__class__.__name__,
            'urn': self.actor_urn,
        }

    def stop(self):
        """
        Stop the actor.

        The actor will finish processing any messages already in its queue
        before stopping. It may not be restarted.
        """
        self.actor_runnable = False
        ActorRegistry.unregister(self.actor_ref)
        logger.debug('Stopped %s', self)

    # pylint: disable = W0703
    def _run(self):
        """
        The actor's main method.

        :class:`pykka.gevent.GeventActor` expects this method to be named
        :meth:`_run`.

        :class:`ThreadingActor` expects this method to be named :meth:`run`.
        """
        self.pre_start()
        self.actor_runnable = True
        while self.actor_runnable:
            message = self.actor_inbox.get()
            try:
                response = self._react(message)
                if 'reply_to' in message:
                    message['reply_to'].set(response)
            except BaseException as exception:
                if 'reply_to' in message:
                    logger.debug('Exception returned from %s to caller:' %
                        self, exc_info=sys.exc_info())
                    message['reply_to'].set_exception(exception)
                else:
                    logger.error('Unhandled exception in %s:' % self,
                        exc_info=sys.exc_info())
        self.post_stop()
    # pylint: enable = W0703

    def pre_start(self):
        """
        Hook for doing any setup that should be done *after* the actor is
        started, but *before* it starts processing messages.

        For :class:`ThreadingActor`, this method is executed in the actor's own
        thread, while :meth:`__init__` is executed in the thread that created
        the actor.
        """
        pass

    def post_stop(self):
        """
        Hook for doing any cleanup that should be done *after* the actor has
        processed the last message, and *before* the actor stops.

        For :class:`ThreadingActor` this method is executed in the actor's own
        thread, immediately before the thread exits.
        """
        pass

    def _react(self, message):
        """Reacts to messages sent to the actor."""
        if message.get('command') == 'pykka_get_attributes':
            return self._get_attributes()
        if message.get('command') == 'pykka_stop':
            return self.stop()
        if message.get('command') == 'pykka_call':
            callee = self._get_attribute_from_path(message['attr_path'])
            return callee(*message['args'], **message['kwargs'])
        if message.get('command') == 'pykka_getattr':
            attr = self._get_attribute_from_path(message['attr_path'])
            return attr
        if message.get('command') == 'pykka_setattr':
            parent_attr = self._get_attribute_from_path(
                message['attr_path'][:-1])
            attr_name = message['attr_path'][-1]
            return setattr(parent_attr, attr_name, message['value'])
        return self.react(message)

    def react(self, message):
        """
        May be implemented for the actor to handle non-proxy messages.

        Messages where the value of the 'command' key matches 'pykka_*' are
        reserved for internal use in Pykka.

        :param message: the message to handle
        :type message: picklable dict

        :returns: anything that should be sent as a reply to the sender
        """
        raise NotImplementedError

    def _is_exposable_attribute(self, attr_name):
        """
        Returns true for any attribute name that may be exposed through
        :class:`ActorProxy`.
        """
        return not attr_name.startswith('_')

    def _is_traversable_attribute(self, attr):
        """
        Returns true for any attribute that may be traversed from another
        actor through :class:`ActorProxy`.
        """
        return hasattr(attr, 'pykka_traversable')

    def _get_attribute_from_path(self, attr_path):
        """
        Traverses the path and returns the attribute at the end of the path.
        """
        attr = self
        for attr_name in attr_path:
            attr = getattr(attr, attr_name)
        return attr

    def _get_attributes(self):
        """Gathers attribute information needed by :class:`ActorProxy`."""
        result = {}
        attr_paths_to_visit = [[attr_name] for attr_name in dir(self)]
        while attr_paths_to_visit:
            attr_path = attr_paths_to_visit.pop(0)
            if self._is_exposable_attribute(attr_path[-1]):
                attr = self._get_attribute_from_path(attr_path)
                result[tuple(attr_path)] = {
                    # NOTE isinstance(attr, collections.Callable), as
                    # recommended by 2to3, does not work on CPython 2.6.4 if
                    # the attribute is an Queue.Queue, but works on 2.6.6.
                    'callable': callable(attr),
                    'traversable': self._is_traversable_attribute(attr),
                }
                if self._is_traversable_attribute(attr):
                    for attr_name in dir(attr):
                        attr_paths_to_visit.append(attr_path + [attr_name])
        return result


# pylint: disable = R0901
class ThreadingActor(Actor, threading.Thread):
    """
    :class:`ThreadingActor` implements :class:`Actor` using regular Python
    threads.

    This implementation is slower than :class:`pykka.gevent.GeventActor`, but
    can be used in a process with other threads that are not Pykka actors.
    """

    _superclass = threading.Thread
    _future_class = ThreadingFuture

    def _new_actor_inbox(self):
        return queue.Queue()

    def run(self):
        return Actor._run(self)

    def react(self, message):
        raise NotImplementedError
# pylint: enable = R0901


class ActorRef(object):
    """
    Reference to a running actor which may safely be passed around.

    :class:`ActorRef` instances are returned by :meth:`Actor.start` and the
    lookup methods in :class:`pykka.registry.ActorRegistry`. You should never
    need to create :class:`ActorRef` instances yourself.

    :param actor: the actor to wrap
    :type actor: :class:`Actor`
    """

    #: See :attr:`Actor.actor_urn`
    actor_urn = None

    def __init__(self, actor):
        self.actor_urn = actor.actor_urn
        self.actor_class = actor.__class__
        self.actor_inbox = actor.actor_inbox
        # pylint: disable = W0212
        self._future_class = actor._future_class
        # pylint: enable = W0212

    def __repr__(self):
        return '<ActorRef for %s>' % str(self)

    def __str__(self):
        return '%(class)s (%(urn)s)' % {
            'urn': self.actor_urn,
            'class': self.actor_class.__name__,
        }

    def is_alive(self):
        """
        Check if actor is alive.

        As this check is just based on the actor being registered in the actor
        registry or, the actor is not guaranteed to be alive and responding
        even though :meth:`is_alive` returns :class:`True`.

        :return:
            Returns :class:`True` if actor is alive, :class:`False` otherwise.
        """
        return ActorRegistry.get_by_urn(self.actor_urn) is not None

    def send_one_way(self, message):
        """
        Send message to actor without waiting for any response.

        Will generally not block, but if the underlying queue is full it will
        block until a free slot is available.

        :param message: message to send
        :type message: picklable dict

        :raise: :exc:`pykka.ActorDeadError` if actor is not available
        :return: nothing
        """
        if not self.is_alive():
            raise ActorDeadError('%s not found' % self)
        self.actor_inbox.put(message)

    def send_request_reply(self, message, block=True, timeout=None):
        """
        Send message to actor and wait for the reply.

        The message must be a picklable dict.
        If ``block`` is :class:`False`, it will immediately return a
        :class:`pykka.future.Future` instead of blocking.

        If ``block`` is :class:`True`, and ``timeout`` is :class:`None`, as
        default, the method will block until it gets a reply, potentially
        forever. If ``timeout`` is an integer or float, the method will wait
        for a reply for ``timeout`` seconds, and then raise
        :exc:`pykka.future.Timeout`.

        :param message: message to send
        :type message: picklable dict

        :param block: whether to block while waiting for a reply
        :type block: boolean

        :param timeout: seconds to wait before timeout if blocking
        :type timeout: float or :class:`None`

        :raise: :exc:`pykka.ActorDeadError` if actor is not available
        :return: :class:`pykka.future.Future` or response
        """
        future = self._future_class()
        message['reply_to'] = future
        self.send_one_way(message)
        if block:
            return future.get(timeout=timeout)
        else:
            return future

    def stop(self, block=True, timeout=None):
        """
        Send a message to the actor, asking it to stop.

        ``block`` and ``timeout`` works as for :meth:`send_request_reply`.

        :return: :class:`True` if actor is stopped. :class:`False` if actor was
            already dead.
        """
        if self.is_alive():
            self.send_request_reply({'command': 'pykka_stop'}, block, timeout)
            return True
        else:
            return False

    def proxy(self):
        """
        Wraps the :class:`ActorRef` in an :class:`pykka.proxy.ActorProxy`.

        Using this method like this::

            proxy = AnActor.start().proxy()

        is analogous to::

            proxy = ActorProxy(AnActor.start())

        :raise: :exc:`pykka.ActorDeadError` if actor is not available
        :return: :class:`pykka.proxy.ActorProxy`
        """
        return ActorProxy(self)
