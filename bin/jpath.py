import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))

import jmespath
import jmespath.exceptions
import jmespath.functions
from splunklib.searchcommands import Configuration, dispatch, Option, StreamingCommand, validators


class JmespathSplunkFunctions(jmespath.functions.Functions):
    """Custom functions for JMSEPath to solve some typical Splunk use cases."""

    @jmespath.functions.signature({"types": ["array", "string"]})
    def _func_from_string(self, arg):
        """Parse a nested JSON text."""
        if arg is None:
            return None
        if isinstance(arg, (list, tuple)):
            return [json.loads(item) for item in arg]
        try:
            return json.loads(arg)
        except Exception:
            return arg

    @jmespath.functions.signature({"types": ["object"]})
    def _func_items(self, arg):
        """See pairs(arg)."""
        return self._func_pairs(arg)

    @jmespath.functions.signature({"types": ["object"]})
    def _func_pairs(self, arg):
        """Create a [key, value] array for each key value pair in an object."""
        return [list(item) for item in arg.items()]

    @jmespath.functions.signature({"types": ["array"]})
    def _func_to_hash(self, array):
        """Build an object from an array of key value pairs.

        If there are duplicates, the last value wins.
        It is the inverse of items().
        """
        object_ = {}
        for item in array:
            try:
                key, value = item
                object_[key] = value
            except Exception:
                pass
        return object_

    @jmespath.functions.signature({"types": ["array"]}, {"types": ["string"]},
                                  {"types": ["string"]})
    def _func_unroll(self, array, key_key, value_key):
        """Build an object from an array of objects with name/value pairs.

        Example: unroll([{"Name": "Pair name", "Value": "Pair value"}], "Name", "Value")
        produces {"Pair name": "Pair value"}.
        """
        object_ = {}
        for item in array:
            try:
                key = item[key_key]
                value = item[value_key]
                if not isinstance(key, str):
                    key = str(key)

                # TODO: User option: Overwrite, keep, or make multivalue.
                if key not in object_:
                    object_[key] = value
                elif isinstance(object_[key], list):
                    object_[key].append(value)
                else:
                    # Opportunistically convert into an array to hold multiple values.
                    # Generally harmful to structured data, but plays nice with Splunk's multivalue
                    # fields.
                    object_[key] = [object_[key], value]
            except KeyError:
                # If either field is missing, just silently move on
                continue
        return object_


@Configuration()
class JMESPath(StreamingCommand):
    error = Option(default="_jmespath_error", require=False, validate=validators.Fieldname())
    default = Option(default=None, require=False)
    input = Option(default="_raw", require=False, validate=validators.Fieldname())
    output = Option(default="jpath", require=False)

    @staticmethod
    def flatten(arg):
        if isinstance(arg, dict):
            yield json.dumps(arg, ensure_ascii=False)
        elif isinstance(arg, (list, tuple)):
            for item in arg:
                if isinstance(item, (list, tuple, dict)):
                    yield json.dumps(item, ensure_ascii=False)
                else:
                    yield str(item)
        else:
            yield str(arg)

    def output_to_field(self, record, values):
        self.write_output(record, self.output, values)

    def output_to_wildcard_fields(self, record, values):
        if isinstance(values, dict):
            for (key, value) in values.items():
                self.write_output(record, self.output.replace("*", key, 1), value)
        else:
            # TODO(aserpi): Find a better way to handle this case.
            self.write_output(record, self.output, values)

    def write_output(self, record, field, values):
        flat_values = list(self.flatten(values))
        if not flat_values:
            flat_values = None
        elif len(flat_values) == 1:
            # Avoid the overhead of multivalue field encoding
            flat_values = flat_values[0]
        self.add_field(record, field, flat_values)

    def stream(self, records):
        if len(self.fieldnames) != 1:
            raise ValueError("Requires exactly one expression argument.")
        jmespath_expr = jmespath.compile(self.fieldnames[0])
        jmespath_options = jmespath.Options(custom_functions=JmespathSplunkFunctions())

        for record in records:
            field = record.get(self.input)
            if isinstance(field, list):
                # TODO: Support multivalue fields
                field = field[0]

            try:
                field_json = json.loads(field)
            except ValueError:
                # TODO(aserpi): Override output with default?
                self.add_field(record, self.error, "Invalid JSON.")
                yield record
                continue

            try:
                jmespath_result = jmespath_expr.search(field_json, options=jmespath_options)
                if jmespath_result is not None:
                    if "*" in self.output:
                        self.output_to_wildcard_fields(record, jmespath_result)
                    else:
                        self.output_to_field(record, jmespath_result)
                elif self.default is not None:
                    self.add_field(record, self.output, self.default)
            except jmespath.exceptions.UnknownFunctionError as e:
                raise ValueError(f"Issue with JMESPath expression: {e}")
            except jmespath.exceptions.JMESPathError as e:
                # FIXME: Not 100% sure about what these errors mean. Should they halt?
                self.add_field(record, self.error, f"JMESPath error: {e}")
            except Exception as e:
                self.add_field(record, self.error, f"Exception: {e}")

            yield record


dispatch(JMESPath, sys.argv, sys.stdin, sys.stdout, __name__)
