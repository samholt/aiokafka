import asyncio
import logging
import collections

from kafka.partitioner.default import DefaultPartitioner
from kafka.protocol.produce import ProduceRequest
from kafka.codec import has_gzip, has_snappy, has_lz4

import aiokafka.errors as Errors
from aiokafka.client import AIOKafkaClient
from aiokafka.errors import (
    MessageSizeTooLargeError, KafkaError, UnknownTopicOrPartitionError)
from aiokafka.record.legacy_records import LegacyRecordBatchBuilder
from aiokafka.structs import TopicPartition
from aiokafka.util import ensure_future

from .message_accumulator import MessageAccumulator

log = logging.getLogger(__name__)


class AIOKafkaProducer(object):
    """A Kafka client that publishes records to the Kafka cluster.

    The producer consists of a pool of buffer space that holds records that
    haven't yet been transmitted to the server as well as a background task
    that is responsible for turning these records into requests and
    transmitting them to the cluster.

    The send() method is asynchronous. When called it adds the record to a
    buffer of pending record sends and immediately returns. This allows the
    producer to batch together individual records for efficiency.

    The 'acks' config controls the criteria under which requests are considered
    complete. The "all" setting will result in waiting for all replicas to
    respond, the slowest but most durable setting.

    The key_serializer and value_serializer instruct how to turn the key and
    value objects the user provides into bytes.

    Arguments:
        bootstrap_servers: 'host[:port]' string (or list of 'host[:port]'
            strings) that the producer should contact to bootstrap initial
            cluster metadata. This does not have to be the full node list.
            It just needs to have at least one broker that will respond to a
            Metadata API Request. Default port is 9092. If no servers are
            specified, will default to localhost:9092.
        client_id (str): a name for this client. This string is passed in
            each request to servers and can be used to identify specific
            server-side log entries that correspond to this client.
            Default: 'aiokafka-producer-#' (appended with a unique number
            per instance)
        key_serializer (callable): used to convert user-supplied keys to bytes
            If not None, called as f(key), should return bytes. Default: None.
        value_serializer (callable): used to convert user-supplied message
            values to bytes. If not None, called as f(value), should return
            bytes. Default: None.
        acks (0, 1, 'all'): The number of acknowledgments the producer requires
            the leader to have received before considering a request complete.
            This controls the durability of records that are sent. The
            following settings are common:

            0: Producer will not wait for any acknowledgment from the server
                at all. The message will immediately be added to the socket
                buffer and considered sent. No guarantee can be made that the
                server has received the record in this case, and the retries
                configuration will not take effect (as the client won't
                generally know of any failures). The offset given back for each
                record will always be set to -1.
            1: The broker leader will write the record to its local log but
                will respond without awaiting full acknowledgement from all
                followers. In this case should the leader fail immediately
                after acknowledging the record but before the followers have
                replicated it then the record will be lost.
            all: The broker leader will wait for the full set of in-sync
                replicas to acknowledge the record. This guarantees that the
                record will not be lost as long as at least one in-sync replica
                remains alive. This is the strongest available guarantee.

            If unset, defaults to acks=1.
        compression_type (str): The compression type for all data generated by
            the producer. Valid values are 'gzip', 'snappy', 'lz4', or None.
            Compression is of full batches of data, so the efficacy of batching
            will also impact the compression ratio (more batching means better
            compression). Default: None.
        max_batch_size (int): Maximum size of buffered data per partition.
            After this amount `send` coroutine will block until batch is
            drained.
            Default: 16384
        linger_ms (int): The producer groups together any records that arrive
            in between request transmissions into a single batched request.
            Normally this occurs only under load when records arrive faster
            than they can be sent out. However in some circumstances the client
            may want to reduce the number of requests even under moderate load.
            This setting accomplishes this by adding a small amount of
            artificial delay; that is, if first request is processed faster,
            than `linger_ms`, producer will wait `linger_ms - process_time`.
            This setting defaults to 0 (i.e. no delay).
        partitioner (callable): Callable used to determine which partition
            each message is assigned to. Called (after key serialization):
            partitioner(key_bytes, all_partitions, available_partitions).
            The default partitioner implementation hashes each non-None key
            using the same murmur2 algorithm as the Java client so that
            messages with the same key are assigned to the same partition.
            When a key is None, the message is delivered to a random partition
            (filtered to partitions with available leaders only, if possible).
        max_request_size (int): The maximum size of a request. This is also
            effectively a cap on the maximum record size. Note that the server
            has its own cap on record size which may be different from this.
            This setting will limit the number of record batches the producer
            will send in a single request to avoid sending huge requests.
            Default: 1048576.
        metadata_max_age_ms (int): The period of time in milliseconds after
            which we force a refresh of metadata even if we haven't seen any
            partition leadership changes to proactively discover any new
            brokers or partitions. Default: 300000
        request_timeout_ms (int): Produce request timeout in milliseconds.
            As it's sent as part of ProduceRequest (it's a blocking call),
            maximum waiting time can be up to 2 * request_timeout_ms.
            Default: 40000.
        retry_backoff_ms (int): Milliseconds to backoff when retrying on
            errors. Default: 100.
        api_version (str): specify which kafka API version to use.
            If set to 'auto', will attempt to infer the broker version by
            probing various APIs. Default: auto
        security_protocol (str): Protocol used to communicate with brokers.
            Valid values are: PLAINTEXT, SSL. Default: PLAINTEXT.
        ssl_context (ssl.SSLContext): pre-configured SSLContext for wrapping
            socket connections. Directly passed into asyncio's
            `create_connection`_. For more information see :ref:`ssl_auth`.
            Default: None.

    Note:
        Many configuration parameters are taken from the Java client:
        https://kafka.apache.org/documentation.html#producerconfigs
    """
    _PRODUCER_CLIENT_ID_SEQUENCE = 0

    _COMPRESSORS = {
        'gzip': (has_gzip, LegacyRecordBatchBuilder.CODEC_GZIP),
        'snappy': (has_snappy, LegacyRecordBatchBuilder.CODEC_SNAPPY),
        'lz4': (has_lz4, LegacyRecordBatchBuilder.CODEC_LZ4),
    }

    def __init__(self, *, loop, bootstrap_servers='localhost',
                 client_id=None,
                 metadata_max_age_ms=300000, request_timeout_ms=40000,
                 api_version='auto', acks=1,
                 key_serializer=None, value_serializer=None,
                 compression_type=None, max_batch_size=16384,
                 partitioner=DefaultPartitioner(), max_request_size=1048576,
                 linger_ms=0, send_backoff_ms=100,
                 retry_backoff_ms=100, security_protocol="PLAINTEXT",
                 ssl_context=None):
        if acks not in (0, 1, -1, 'all'):
            raise ValueError("Invalid ACKS parameter")
        if compression_type not in ('gzip', 'snappy', 'lz4', None):
            raise ValueError("Invalid compression type!")
        if compression_type:
            checker, compression_attrs = self._COMPRESSORS[compression_type]
            if not checker():
                raise RuntimeError("Compression library for {} not found"
                                   .format(compression_type))
        else:
            compression_attrs = 0

        if api_version not in (
                'auto', '0.10', '0.9', '0.8.2', '0.8.1', '0.8.0'):
            raise ValueError("Unsupported Kafka version")

        self._PRODUCER_CLIENT_ID_SEQUENCE += 1
        if client_id is None:
            client_id = 'aiokafka-producer-%s' % \
                self._PRODUCER_CLIENT_ID_SEQUENCE

        if acks == 'all':
            acks = -1
        self._acks = acks
        self._key_serializer = key_serializer
        self._value_serializer = value_serializer
        self._compression_type = compression_type
        self._partitioner = partitioner
        self._max_request_size = max_request_size
        self._request_timeout_ms = request_timeout_ms

        self.client = AIOKafkaClient(
            loop=loop, bootstrap_servers=bootstrap_servers,
            client_id=client_id, metadata_max_age_ms=metadata_max_age_ms,
            request_timeout_ms=request_timeout_ms,
            retry_backoff_ms=retry_backoff_ms,
            api_version=api_version, security_protocol=security_protocol,
            ssl_context=ssl_context)
        self._metadata = self.client.cluster
        self._message_accumulator = MessageAccumulator(
            self._metadata, max_batch_size, compression_attrs,
            self._request_timeout_ms / 1000, loop)
        self._sender_task = None
        self._in_flight = set()
        self._closed = False
        self._loop = loop
        self._retry_backoff = retry_backoff_ms / 1000
        self._linger_time = linger_ms / 1000
        self._producer_magic = 0

    @asyncio.coroutine
    def start(self):
        """Connect to Kafka cluster and check server version"""
        log.debug("Starting the Kafka producer")  # trace
        yield from self.client.bootstrap()

        if self._compression_type == 'lz4':
            assert self.client.api_version >= (0, 8, 2), \
                'LZ4 Requires >= Kafka 0.8.2 Brokers'

        self._sender_task = ensure_future(
            self._sender_routine(), loop=self._loop)
        self._message_accumulator.set_api_version(self.client.api_version)
        self._producer_magic = 0 if self.client.api_version < (0, 10) else 1
        log.debug("Kafka producer started")

    @asyncio.coroutine
    def flush(self):
        """Wait untill all batches are Delivered and futures resolved"""
        yield from self._message_accumulator.flush()

    @asyncio.coroutine
    def stop(self):
        """Flush all pending data and close all connections to kafka cluster"""
        if self._closed:
            return
        self._closed = True

        yield from self._message_accumulator.close()

        if self._sender_task:
            self._sender_task.cancel()
            yield from self._sender_task

        yield from self.client.close()
        log.debug("The Kafka producer has closed.")

    @asyncio.coroutine
    def partitions_for(self, topic):
        """Returns set of all known partitions for the topic."""
        return (yield from self.client._wait_on_metadata(topic))

    @asyncio.coroutine
    def send(self, topic, value=None, key=None, partition=None,
             timestamp_ms=None):
        """Publish a message to a topic.

        Arguments:
            topic (str): topic where the message will be published
            value (optional): message value. Must be type bytes, or be
                serializable to bytes via configured value_serializer. If value
                is None, key is required and message acts as a 'delete'.
                See kafka compaction documentation for more details:
                http://kafka.apache.org/documentation.html#compaction
                (compaction requires kafka >= 0.8.1)
            partition (int, optional): optionally specify a partition. If not
                set, the partition will be selected using the configured
                'partitioner'.
            key (optional): a key to associate with the message. Can be used to
                determine which partition to send the message to. If partition
                is None (and producer's partitioner config is left as default),
                then messages with the same key will be delivered to the same
                partition (but if key is None, partition is chosen randomly).
                Must be type bytes, or be serializable to bytes via configured
                key_serializer.
            timestamp_ms (int, optional): epoch milliseconds (from Jan 1 1970
                UTC) to use as the message timestamp. Defaults to current time.

        Returns:
            asyncio.Future: object that will be set when message is
            processed

        Raises:
            kafka.KafkaTimeoutError: if we can't schedule this record (
                pending buffer is full) in up to `request_timeout_ms`
                milliseconds.

        Note:
            The returned future will wait based on `request_timeout_ms`
            setting. Cancelling the returned future **will not** stop event
            from being sent, but cancelling the ``send`` coroutine itself
            **will**.
        """
        assert value is not None or self.client.api_version >= (0, 8, 1), (
            'Null messages require kafka >= 0.8.1')
        assert not (value is None and key is None), \
            'Need at least one: key or value'

        # first make sure the metadata for the topic is available
        yield from self.client._wait_on_metadata(topic)

        key_bytes, value_bytes = self._serialize(topic, key, value)
        partition = self._partition(topic, partition, key, value,
                                    key_bytes, value_bytes)

        tp = TopicPartition(topic, partition)
        log.debug("Sending (key=%s value=%s) to %s", key, value, tp)

        fut = yield from self._message_accumulator.add_message(
            tp, key_bytes, value_bytes, self._request_timeout_ms / 1000,
            timestamp_ms=timestamp_ms)
        return fut

    @asyncio.coroutine
    def send_and_wait(self, topic, value=None, key=None, partition=None,
                      timestamp_ms=None):
        """Publish a message to a topic and wait the result"""
        future = yield from self.send(
            topic, value, key, partition, timestamp_ms)
        return (yield from future)

    @asyncio.coroutine
    def _sender_routine(self):
        """ Background task, that sends pending batches to leader nodes for
        batch's partition. This incapsulates same logic as Java's `Sender`
        background thread. Because we use asyncio this is more event based
        loop, rather than counting timeout till next possible even like in
        Java.

            The procedure:
            * Group pending batches by partition leaders (write nodes)
            * Ignore not ready (disconnected) and nodes, that already have a
              pending request.
            * If we have unknown leaders for partitions, we request a metadata
              update.
            * Wait for any event, that can change the above procedure, like
              new metadata or pending send is finished and a new one can be
              done.
        """
        tasks = set()
        try:
            while True:
                batches, unknown_leaders_exist = \
                    self._message_accumulator.drain_by_nodes(
                        ignore_nodes=self._in_flight)

                # create produce task for every batch
                for node_id, batches in batches.items():
                    task = ensure_future(
                        self._send_produce_req(node_id, batches),
                        loop=self._loop)
                    self._in_flight.add(node_id)
                    tasks.add(task)

                if unknown_leaders_exist:
                    # we have at least one unknown partition's leader,
                    # try to update cluster metadata and wait backoff time
                    fut = self.client.force_metadata_update()
                    waiters = tasks.union([fut])
                else:
                    fut = self._message_accumulator.data_waiter()
                    waiters = tasks.union([fut])

                # wait when:
                # * At least one of produce task is finished
                # * Data for new partition arrived
                # * Metadata update if partition leader unknown
                done, _ = yield from asyncio.wait(
                    waiters,
                    return_when=asyncio.FIRST_COMPLETED,
                    loop=self._loop)

                # done tasks should never produce errors, if they are it's a
                # bug
                for task in done:
                    task.result()

                tasks -= done

        except asyncio.CancelledError:
            # done tasks should never produce errors, if they are it's a bug
            for task in tasks:
                yield from task
        except Exception:  # pragma: no cover
            log.error("Unexpected error in sender routine", exc_info=True)

    @asyncio.coroutine
    def _send_produce_req(self, node_id, batches):
        """ Create produce request to node
        If producer configured with `retries`>0 and produce response contain
        "failed" partitions produce request for this partition will try
        resend to broker `retries` times with `retry_timeout_ms` timeouts.

        Arguments:
            node_id (int): kafka broker identifier
            batches (dict): dictionary of {TopicPartition: MessageBatch}
        """
        t0 = self._loop.time()

        topics = collections.defaultdict(list)
        for tp, batch in batches.items():
            topics[tp.topic].append(
                (tp.partition, batch.get_data_buffer())
            )

        if self.client.api_version >= (0, 10):
            version = 2
        elif self.client.api_version == (0, 9):
            version = 1
        else:
            version = 0

        request = ProduceRequest[version](
            required_acks=self._acks,
            timeout=self._request_timeout_ms,
            topics=list(topics.items()))

        reenqueue = []
        try:
            response = yield from self.client.send(node_id, request)
        except KafkaError as err:
            log.warning(
                "Got error produce response: %s", err)
            if getattr(err, "invalid_metadata", False):
                self.client.force_metadata_update()

            for batch in batches.values():
                if not self._can_retry(err, batch):
                    batch.failure(exception=err)
                else:
                    reenqueue.append(batch)
        else:
            # noacks, just mark batches as "done"
            if request.required_acks == 0:
                for batch in batches.values():
                    batch.done_noack()
            else:
                for topic, partitions in response.topics:
                    for partition_info in partitions:
                        if response.API_VERSION < 2:
                            partition, error_code, offset = partition_info
                            # Mimic CREATE_TIME to take user provided timestamp
                            timestamp = -1
                        else:
                            partition, error_code, offset, timestamp = \
                                partition_info
                        tp = TopicPartition(topic, partition)
                        error = Errors.for_code(error_code)
                        batch = batches.pop(tp, None)
                        if batch is None:
                            continue

                        if error is Errors.NoError:
                            batch.done(offset, timestamp)
                        elif not self._can_retry(error(), batch):
                            batch.failure(exception=error())
                        else:
                            log.warning(
                                "Got error produce response on topic-partition"
                                " %s, retrying. Error: %s", tp, error)
                            # Ok, we can retry this batch
                            if getattr(error, "invalid_metadata", False):
                                self.client.force_metadata_update()
                            reenqueue.append(batch)

        if reenqueue:
            # Wait backoff before reequeue
            yield from asyncio.sleep(self._retry_backoff, loop=self._loop)

            for batch in reenqueue:
                self._message_accumulator.reenqueue(batch)
            # If some error started metadata refresh we have to wait before
            # trying again
            yield from self.client._maybe_wait_metadata()

        # if batches for node is processed in less than a linger seconds
        # then waiting for the remaining time
        sleep_time = self._linger_time - (self._loop.time() - t0)
        if sleep_time > 0:
            yield from asyncio.sleep(sleep_time, loop=self._loop)

        self._in_flight.remove(node_id)

    def _can_retry(self, error, batch):
        if batch.expired():
            return False
        # XXX: remove unknown topic check as we fix
        #      https://github.com/dpkp/kafka-python/issues/1155
        if error.retriable or isinstance(error, UnknownTopicOrPartitionError)\
                or error is UnknownTopicOrPartitionError:
            return True
        return False

    def _serialize(self, topic, key, value):
        if self._key_serializer:
            serialized_key = self._key_serializer(key)
        else:
            serialized_key = key
        if self._value_serializer:
            serialized_value = self._value_serializer(value)
        else:
            serialized_value = value

        message_size = LegacyRecordBatchBuilder.record_overhead(
            self._producer_magic)
        if serialized_key is not None:
            message_size += len(serialized_key)
        if serialized_value is not None:
            message_size += len(serialized_value)
        if message_size > self._max_request_size:
            raise MessageSizeTooLargeError(
                "The message is %d bytes when serialized which is larger than"
                " the maximum request size you have configured with the"
                " max_request_size configuration" % message_size)

        return serialized_key, serialized_value

    def _partition(self, topic, partition, key, value,
                   serialized_key, serialized_value):
        if partition is not None:
            assert partition >= 0
            assert partition in self._metadata.partitions_for_topic(topic), \
                'Unrecognized partition'
            return partition

        all_partitions = list(self._metadata.partitions_for_topic(topic))
        available = list(self._metadata.available_partitions_for_topic(topic))
        return self._partitioner(
            serialized_key, all_partitions, available)

    def create_batch(self):
        """Create and return an empty BatchBuilder.

        The batch is not queued for send until submission to ``send_batch``.

        Returns:
            BatchBuilder: empty batch to be filled and submitted by the caller.
        """
        return self._message_accumulator.create_builder()

    @asyncio.coroutine
    def send_batch(self, batch, topic, *, partition):
        """Submit a BatchBuilder for publication.

        Arguments:
            batch (BatchBuilder): batch object to be published.
            topic (str): topic where the batch will be published.
            partition (int): partition where this batch will be published.

        Returns:
            asyncio.Future: object that will be set when the batch is
                delivered.
        """
        partition = self._partition(topic, partition, None, None, None, None)
        tp = TopicPartition(topic, partition)
        log.debug("Sending batch to %s", tp)
        future = yield from self._message_accumulator.add_batch(
            batch, tp, self._request_timeout_ms / 1000)
        return future
