# coding=utf-8
# Copyright 2021 TF.Text Authors.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Library of ops to truncate segments."""
import abc

from tensorflow.python.framework import dtypes
from tensorflow.python.framework import ops
from tensorflow.python.ops import array_ops
from tensorflow.python.ops import math_ops
from tensorflow.python.ops.ragged import ragged_array_ops
from tensorflow.python.ops.ragged import ragged_tensor
from tensorflow_text.python.ops import item_selector_ops


class Trimmer(object):
  """Truncates a list of segments using a pre-determined truncation strategy.
  """

  def trim(self, segments):
    """Truncate the list of `segments`.

    Truncate the list of `segments` using the truncation strategy defined by
    `generate_masks`.

    Args:
      segments: A list of `RaggedTensor`s w/ shape [num_batch, (num_items)].

    Returns:
      a list of `RaggedTensor`s with len(segments) number of items and where
      each item has the same shape as its counterpart in `segments` and
      with unwanted values dropped. The values are dropped according to the
      `TruncationStrategy` defined.
    """
    with ops.name_scope("Trimmer/Trim"):
      segments = [ragged_tensor.convert_to_tensor_or_ragged_tensor(s)
                  for s in segments]
      truncate_masks = self.generate_mask(segments)
      truncated_segments = [
          ragged_array_ops.boolean_mask(
              seg, mask.with_row_splits_dtype(seg.row_splits.dtype))
          for seg, mask in zip(segments, truncate_masks)
      ]
      return truncated_segments

  @abc.abstractmethod
  def generate_masks(self, segments):
    """Generates a boolean mask specifying which portions of `segments` to drop.

    Users should be able to use the results of generate_masks() to drop items
    in segments using `tf.ragged.boolean_mask(seg, mask)`.

    Args:
      segments: A list of `RaggedTensor` each w/ a shape of [num_batch,
        (num_items)].

    Returns:
      a list with len(segments) number of items and where each item is a
      `RaggedTensor` with the same shape as its counterpart in `segments` and
      with a boolean dtype where each value is True if the corresponding
      value in `segments` should be kept and False if it should be dropped
      instead.
    """
    raise NotImplementedError()


def _get_row_lengths(segments, axis=-1):
  axis = array_ops.get_positive_axis(axis, segments.shape.ndims) - 1
  foo = ragged_tensor.RaggedTensor.from_nested_row_lengths(
      segments.nested_row_lengths()[axis],
      segments.nested_row_lengths()[:axis])
  for _ in range(axis):
    foo = math_ops.reduce_sum(foo, -1)
  return foo


class WaterfallTrimmer(Trimmer):
  """A `Trimmer` that allocates a length budget to segments in order.

  A `Trimmer` that allocates a length budget to segments in order, then
  truncates large sequences using a waterfall strategy, then drops elements in a
  sequence according to a max sequence length budget. See `generate_mask()`
  for more details.
  """

  def __init__(self, max_seq_length, axis=-1):
    """Creates an instance of `WaterfallTruncator`.

    Args:
      max_seq_length: a scalar `Tensor` or a 1D `Tensor` of type int32 that
        describes the number max number of elements allowed in a batch. If a
        scalar is provided, the value is broadcasted and applied to all values
        across the batch.
      axis: Axis to apply trimming on.
    """
    self._max_seq_length = max_seq_length
    self._axis = axis

  def generate_mask(self, segments):
    """Calculates a truncation mask given a per-batch budget.

    Calculate a truncation mask given a budget of the max number of items for
    each or all batch row. The allocation of the budget is done using a
    'waterfall' algorithm. This algorithm allocates quota in a left-to-right
    manner and fill up the buckets until we run out of budget.

    For example if the budget of [5] and we have segments of size
    [3, 4, 2], the truncate budget will be allocated as [3, 2, 0].

    The budget can be a scalar, in which case the same budget is broadcasted
    and applied to all batch rows. It can also be a 1D `Tensor` of size
    `batch_size`, in which each batch row i will have a budget corresponding to
    `per_batch_quota[i]`.

    Args:
      segments: A list of `RaggedTensor` each w/ a shape of [num_batch,
        (num_items)].
    Returns:
      a list with len(segments) of `RaggedTensor`s, see superclass for details.
    """
    with ops.name_scope("WaterfallTrimmer/generate_mask"):
      segment_row_lengths = [_get_row_lengths(s, self._axis) for s in segments]
      segment_row_lengths = array_ops.stack(segment_row_lengths, axis=-1)

      # Broadcast budget to match the rank of segments[0]
      budget = ops.convert_to_tensor(self._max_seq_length)
      for _ in range(segments[0].shape.ndims - budget.shape.ndims):
        budget = array_ops.expand_dims(budget, -1)

      # Compute the allocation for each segment using a `waterfall` algorithm
      segment_lengths = math_ops.cast(segment_row_lengths, dtypes.int32)
      budget = math_ops.cast(budget, dtypes.int32)
      leftover_budget = math_ops.cumsum(
          -1 * segment_lengths, exclusive=False, axis=-1) + budget
      leftover_budget = segment_lengths + math_ops.minimum(leftover_budget, 0)
      results = math_ops.maximum(leftover_budget, 0)

      # Translate the results into boolean masks that match the shape of each
      # segment
      results = array_ops.unstack(results, axis=-1)
      item_selectors = [
          item_selector_ops.FirstNItemSelector(i) for i in results
      ]
      return [
          i.get_selectable(s, self._axis)
          for s, i in zip(segments, item_selectors)
      ]


class RoundRobinTrimmer(Trimmer):
  """A `Trimmer` that allocates a length budget to segments via round robin.

  A `Trimmer` that allocates a length budget to segments using a round robin
  strategy, then drops elements outside of the segment's allocated budget.
  See `generate_mask()` for more details.
  """

  def __init__(self, max_seq_length, axis=-1):
    """Creates an instance of `RoundRobinTrimmer`.

    Args:
      max_seq_length: a scalar `Tensor` int32 that describes the number max
        number of elements allowed in a batch.
      axis: Axis to apply trimming on.
    """
    self._max_seq_length = max_seq_length
    self._axis = axis

  def generate_mask(self, segments):
    """Calculates a truncation mask given a per-batch budget.

    Calculate a truncation mask given a budget of the max number of items for
    each or all batch row. The allocation of the budget is done using a
    'round robin' algorithm. This algorithm allocates quota in each bucket,
    left-to-right repeatedly until all the buckets are filled.

    For example if the budget of [5] and we have segments of size
    [3, 4, 2], the truncate budget will be allocated as [2, 2, 1].

    Args:
      segments: A list of `RaggedTensor` each w/ a shape of [num_batch,
        (num_items)].

    Returns:
      a list with len(segments) of `RaggedTensor`s, see superclass for details.
    """
    with ops.name_scope("RoundRobinTrimmer/generate_mask"):
      segment_row_lengths = [_get_row_lengths(s, self._axis) for s in segments]
      segment_row_lengths = array_ops.stack(segment_row_lengths, axis=-1)

      budget = ops.convert_to_tensor(self._max_seq_length)
      # Broadcast and make `budget` match the shape of `segment_row_lengths`
      budget = budget + math_ops.cast(0 * segment_row_lengths, dtypes.int32)

      # Take the budget and equally distribute it among all the segments.
      budget_per_segment = math_ops.cast(budget / len(segments), dtypes.int32)
      budget_per_segment = math_ops.cast(budget_per_segment, dtypes.int64)

      # Figure out the min num of elements per segment
      min_row_length = math_ops.reduce_min(segment_row_lengths, axis=-1)
      for _ in range(segment_row_lengths.shape.ndims -
                     min_row_length.shape.ndims):
        min_row_length = array_ops.expand_dims(min_row_length, -1)

      # We either deduct the min across a row, or the equally distributed budget
      socialism = math_ops.minimum(min_row_length, budget_per_segment)
      leftover_segment_lengths = segment_row_lengths - socialism

      # Update the new budget w/ everyone's equal share removed
      budget = budget - math_ops.cast(socialism * len(segments), dtypes.int32)
      segment_row_lengths = leftover_segment_lengths

      # Compute the remaining allocation for each segment using a `waterfall`
      # algorithm
      segment_lengths = math_ops.cast(segment_row_lengths, dtypes.int32)
      budget = math_ops.cast(budget, dtypes.int32)
      leftover_budget = math_ops.cumsum(
          -1 * segment_lengths, exclusive=False, axis=-1) + budget
      leftover_budget = segment_lengths + math_ops.minimum(leftover_budget, 0)
      results = math_ops.maximum(leftover_budget, 0)
      results = results + math_ops.cast(socialism, dtypes.int32)
      # Translate the results into boolean masks that match the shape of each
      # segment
      results = array_ops.unstack(results, axis=-1)

      item_selectors = [
          item_selector_ops.FirstNItemSelector(i) for i in results
      ]
      return [
          i.get_selectable(s, self._axis)
          for s, i in zip(segments, item_selectors)
      ]
