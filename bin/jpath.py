import json
import re
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))

import jmespath
import jmespath.exceptions
import jmespath.functions
from splunklib.searchcommands import Configuration, dispatch, Option, StreamingCommand, validators


class JmespathSplunkFunctions(jmespath.functions.Functions):
    """Custom functions for JMSEPath to solve some typical Splunk use cases."""

    @jmespath.functions.signature({'types': ['object']})
    def _func_items(self, h):
        """Create a [name, value] array for each name/value pair in an object."""
        return [list(item) for item in h.items()]

    @jmespath.functions.signature({'types': ['array']})
    def _func_to_hash(self, array):
        """Build an object from an array of name/value pairs.

        If there are duplicates, the last value wins.
        It is the inverse of items().
        """
        h = {}
        for item in array:
            try:
                key, val = item
                h[key] = val
            except Exception:
                pass
        return h

    @jmespath.functions.signature({'types': ['string', 'array']})
    def _func_from_string(self, s):
        """Parse a nested JSON text."""
        if s is None:
            return None
        if isinstance(s, (list, tuple)):
            return [json.loads(i) for i in s]
        try:
            return json.loads(s)
        except Exception:
            return s

    @jmespath.functions.signature({'types': ['array']}, {'types': ['string']},
                                  {'types': ['string']})
    def _func_unroll(self, objs, key, value):
        """Build an object from an array of objects with name/value pairs.

        Example: unroll([{"Name": "Pair name", "Value": "Pair value"}], "Name", "Value")
        produces {"Pair name": "Pair value"}.
        """
        d = {}
        for item in objs:
            try:
                k = item[key]
                v = item[value]
                if not isinstance(k, str):
                    k = str(k)
                # TODO: User option: Overwrite, or make multivalue.
                # Possibly just make 2 different functions?
                if k not in d:
                    d[k] = v
                else:
                    # Opportunistically turn this into a container to hold more than on value.
                    # Generally harmful to structured data, but plays nice with Splunk's mvfields
                    if not isinstance(d[k], list):
                        d[k] = [d[k]]
                    d[k].append(v)
            except KeyError:
                # If either field is missing, just silently move on
                continue
        return d


def flatten(container):
    if isinstance(container, dict):
        yield json.dumps(container, ensure_ascii=False)
    elif isinstance(container, (list, tuple)):
        for i in container:
            if isinstance(i, (list, tuple, dict)):
                yield json.dumps(i, ensure_ascii=False)
            else:
                yield str(i)
    else:
        yield str(container)


# TODO(aserpi): Refactor
def output_to_field(values, output, record):
    content = list(flatten(values))
    if not content:
        content = None
        # TODO(aserpi): Remove.
        record[output] = None
    elif len(content) == 1:
        # Avoid the overhead of multivalue field encoding
        content = content[0]
    record[output] = content


# TODO(aserpi): Refactor
def output_to_wildcard(values, output, record):
    if values is None:
        # Don't bother to make any fields
        return

    if isinstance(values, dict):
        for (key, value) in values.items():
            output = output.replace("*", key, 1)
            if isinstance(value, (list, tuple)):
                if not value:
                    value = None
                elif len(value) == 1:
                    # Unroll to better match Splunk's default handling of multivalue fields
                    value = value[0]
                else:
                    value = json.dumps(value, ensure_ascii=False)
                record[output] = value
            elif isinstance(value, dict):
                record[output] = json.dumps(value, ensure_ascii=False)
            else:
                record[output] = value
    else:
        # Fallback to using a silly name since there's no key to work with.
        # Maybe users didn't mean to use '*' in output, or possibly a record/data specific issue.
        # TODO(aserpi): Find a better way to handle this case.
        record[output] = json.dumps(values, ensure_ascii=False)


@Configuration()
class JMESPath(StreamingCommand):
    error = Option(default="_jmespath_error", require=False, validate=validators.Fieldname())
    default = Option(default=None, require=False)
    input = Option(default="_raw", require=False, validate=validators.Fieldname())
    output = Option(default="jpath", require=False)

    def stream(self, records):
        if len(self.fieldnames) != 1:
            raise ValueError("Requires exactly one expression argument.")
        apply_output = output_to_wildcard if "*" in self.output else output_to_field
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
                record[self.error] = "Invalid JSON."
                yield record
                continue

            try:
                output = jmespath_expr.search(field_json, options=jmespath_options)
                if output is not None:
                    apply_output(output, self.output, record)
                elif self.default is not None:
                    record[self.output] = self.default
            except jmespath.exceptions.UnknownFunctionError as e:
                raise ValueError(f"Issue with JMESPath expression. {e}")
            except jmespath.exceptions.JMESPathError as e:
                # FIXME: Not 100% sure about what these errors mean. Should they halt?
                record[self.error] = f"JMESPath error: {e}"
            except Exception as e:
                record[self.error] = f"Exception: {e}"

            yield record


dispatch(JMESPath, sys.argv, sys.stdin, sys.stdout, __name__)
