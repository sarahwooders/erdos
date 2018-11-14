import logging
import pickle
import sys

from erdos.data_stream import DataStream
from erdos.message import Message
from erdos.op import Op
from erdos.timestamp import Timestamp
from erdos.utils import frequency


class LogOp(Op):
    """Operator which logs data from input streams to file or stdout.

    Args:
        input_streams (list): list of input streams from which to log.
        name (str): unique name for this operator. Generated by default.
        fmt (str): formatting string for logged messages. See logging module
            documentation for more details.
        date_fmt (str): formatting string for the date. See logging module
            documentation for more details.
        console_output (bool): whether to print messages to stdout.
        filename (str): file at which to log messages. If empty, messages are
            not logged to file.
        mode (str): mode in which the log file is opened.
        encoding (str): encoding with which the log file is opened.
    """

    def __init__(self,
                 name="",
                 fmt=None,
                 date_fmt=None,
                 console_output=True,
                 filename="",
                 mode="a",
                 encoding=None):
        super(LogOp, self).__init__(name)
        self.format = fmt
        self.date_format = date_fmt
        self.console_output = console_output
        self.filename = filename
        self.mode = mode
        self.encoding = encoding

    @staticmethod
    def setup_streams(input_streams):
        input_streams.add_callback(LogOp.log_input)
        return []

    def log_input(self, msg):
        self.logger.info(msg)

    def execute(self):
        # Set up logger
        self.logger = logging.getLogger(self.name)
        self.logger.setLevel(logging.INFO)

        formatter = logging.Formatter(self.format, self.date_format)

        if self.console_output:
            stream_handler = logging.StreamHandler(sys.stdout)
            stream_handler.setFormatter(formatter)
            self.logger.addHandler(stream_handler)

        if self.filename:
            file_handler = logging.FileHandler(self.filename, self.mode,
                                               self.encoding)
            file_handler.setFormatter(formatter)
            self.logger.addHandler(file_handler)
        self.logger.propagate = False
        self.spin()


class RecordOp(Op):
    """Operator which saves serialized data from input streams to file.

    Args:
        filename (str): path to file.
        input_streams (list): list of input streams from which to save data.
        name (str): unique name for this operator. Generated by default.
    """

    def __init__(self, name, filename):
        super(RecordOp, self).__init__(name)
        self.filename = filename

    @staticmethod
    def setup_streams(input_streams, filter):
        if filter:
            input_streams = input_streams.filter_name(filter)
        input_streams.add_callback(RecordOp.record_data)
        return []

    def record_data(self, msg):
        pickle.dump(msg, self._file)

    def execute(self):
        self._file = open(self.filename, "wb")

        # Write input stream info
        input_stream_info = [(input_stream.data_type, input_stream.name)
                             for input_stream in self.input_streams]
        pickle.dump(input_stream_info, self._file)
        self.spin()


class ReplayOp(Op):
    """Operator which replays saved data from file to an output stream.

    Args:
        filename (str): path to file.
        frequency (int): rate at which the operator publishes data. If 0, the
            operator publishes data as soon as it is read from file.
        name (str): unique name for this operator. Generated by default.
    """

    def __init__(self, filename, frequency=0, name="replay_op"):
        super(ReplayOp, self).__init__(name)
        self.filename = filename
        self.frequency = frequency

    @staticmethod
    def setup_streams(input_streams, filename):
        output_streams = []
        with open(filename, "rb") as f:
            stream_info = pickle.load(f)
            for data_type, name in stream_info:
                output_streams.append(
                    DataStream(data_type=data_type, name=name))
        return output_streams

    def publish_data(self):
        if self._file.closed:
            return
        try:
            msg = pickle.load(self._file)
            self.get_output_stream(msg.stream_name).send(msg)
        except EOFError:
            logging.error("Reached end of input file: {0}".format(
                self.filename))
            self._file.close()

    def execute(self):
        self._file = open(self.filename, "rb")
        pickle.load(self._file)  # Read past the stream names

        if self.frequency:
            publish_data = frequency(self.frequency)(self.publish_data)
            publish_data()
            self.spin()
        else:
            while True:
                self.publish_data()


class WhereOp(Op):
    def __init__(self, name, output_stream_name, where_lambda):
        super(WhereOp, self).__init__(name)
        self._output_stream_name = output_stream_name
        self._where_lambda = where_lambda

    @staticmethod
    def setup_streams(input_streams,
                      output_stream_name,
                      filter_stream_lambda=None):
        input_streams.filter(filter_stream_lambda).add_callback(WhereOp.on_msg)
        return [DataStream(name=output_stream_name)]

    def on_msg(self, msg):
        if self._where_lambda(msg):
            self.get_output_stream(self._output_stream_name).send(msg)


class MapOp(Op):
    def __init__(self, name, output_stream_name, map_lambda):
        super(MapOp, self).__init__(name)
        self._output_stream_name = output_stream_name
        self._map_lambda = map_lambda

    @staticmethod
    def setup_streams(input_streams,
                      output_stream_name,
                      filter_stream_lambda=None):
        input_streams.filter(filter_stream_lambda).add_callback(MapOp.on_msg)
        return [DataStream(name=output_stream_name)]

    def on_msg(self, msg):
        new_data = self._map_lambda(msg)
        new_msg = Message(new_data, msg.timestamp)
        self.get_output_stream(self._output_stream_name).send(new_msg)


class MapManyOp(Op):
    def __init__(self, name, output_stream_name, map_lambda=None):
        super(MapManyOp, self).__init__(name)
        self._output_stream_name = output_stream_name
        self._map_lambda = map_lambda

    @staticmethod
    def setup_streams(input_streams,
                      output_stream_name,
                      filter_stream_lambda=None):
        input_streams.filter(filter_stream_lambda).add_callback(
            MapManyOp.on_msg)
        return [DataStream(name=output_stream_name)]

    def on_msg(self, msg):
        if self._map_lambda:
            data = self._map_lambda(msg)
        else:
            data = msg.data
        try:
            data = iter(msg.data)
        except TypeError:
            data = [data]
        for val in data:
            self.get_output_stream(self._output_stream_name).send(
                Message(val, msg.timestamp))


class ConcatOp(Op):
    def __init__(self, name, output_stream_name):
        super(ConcatOp, self).__init__(name)
        self._output_stream_name = output_stream_name

    @staticmethod
    def setup_streams(input_streams,
                      output_stream_name,
                      filter_stream_lambda=None):
        input_streams.filter(filter_stream_lambda).add_callback(ConcatOp.on_msg)
        return [DataStream(name=output_stream_name)]

    def on_msg(self, msg):
        self.get_output_stream(self._output_stream_name).send(msg)


class UnzipOp(Op):
    def __init__(self, name, output_stream_name1, output_stream_name2):
        super(UnzipOp, self).__init__(name)
        self._output_stream_name1 = output_stream_name1
        self._output_stream_name2 = output_stream_name2

    @staticmethod
    def setup_streams(input_streams,
                      output_stream_name1,
                      output_stream_name2,
                      filter_stream_lambda=None):
        input_streams.filter(filter_stream_lambda).add_callback(UnzipOp.on_msg)
        return [
            DataStream(name=output_stream_name1),
            DataStream(name=output_stream_name2)
        ]

    def on_msg(self, msg):
        (left_val, right_val) = msg.data
        self.get_output_stream(self._output_stream_name1).send(
            Message(left_val, Timestamp(msg.timestamp)))
        self.get_output_stream(self._output_stream_name2).send(
            Message(right_val, Timestamp(msg.timestamp)))
