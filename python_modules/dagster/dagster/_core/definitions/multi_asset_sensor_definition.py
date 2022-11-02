import inspect
import json
import warnings
from collections import OrderedDict, defaultdict
from typing import (
    TYPE_CHECKING,
    Callable,
    Dict,
    Iterable,
    Iterator,
    List,
    Mapping,
    NamedTuple,
    Optional,
    Sequence,
    Set,
    Union,
    cast,
)

import dagster._check as check
from dagster._annotations import experimental, public
from dagster._core.definitions.asset_selection import AssetSelection
from dagster._core.definitions.assets import AssetsDefinition
from dagster._core.definitions.partition import PartitionsDefinition
from dagster._core.definitions.partition_key_range import PartitionKeyRange
from dagster._core.definitions.time_window_partitions import TimeWindowPartitionsDefinition
from dagster._core.errors import (
    DagsterInvalidDefinitionError,
    DagsterInvalidInvocationError,
    DagsterInvariantViolationError,
)
from dagster._core.instance import DagsterInstance
from dagster._core.instance.ref import InstanceRef

from ..decorator_utils import get_function_params
from .events import AssetKey
from .run_request import RunRequest, SkipReason
from .sensor_definition import (
    DefaultSensorStatus,
    RawSensorEvaluationFunctionReturn,
    SensorDefinition,
    SensorEvaluationContext,
    is_context_provided,
)
from .target import ExecutableDefinition
from .utils import check_valid_name

if TYPE_CHECKING:
    from dagster._core.definitions.repository_definition import RepositoryDefinition
    from dagster._core.events.log import EventLogEntry
    from dagster._core.storage.event_log.base import EventLogRecord

MAX_NUM_UNCONSUMED_EVENTS = 25


class MultiAssetSensorAssetCursorComponent(
    NamedTuple(
        "_MultiAssetSensorAssetCursorComponent",
        [
            ("latest_consumed_event_partition", Optional[str]),
            ("latest_consumed_event_id", Optional[int]),
            ("trailing_unconsumed_partitioned_event_ids", Dict[str, int]),
        ],
    )
):
    """A cursor component that is used to track the cursor for a particular asset in a multi-asset
    sensor.

    Here's an illustration to help explain how this representation works:

    partition_1  ---|----------a----
    partition_2  -t-----|-x---------
    partition_3  ----t------|---a---


    The "|", "a", "t", and "x" characters represent materialization events.
    The x-axis is storage_id, which is basically time. The cursor has been advanced to the "|" event
    for each partition. latest_evaluated_event_partition would be "partition_3", and
    "latest_evaluated_event_id" would be the storage_id of the "|" event for partition_3.

    The "t" events aren't directly represented in the cursor, because they trail the event that the
    the cursor for their partition has advanced to. The "a" events aren't directly represented
    in the cursor, because they occurred after the "latest_evaluated_event_id".  The "x" event is
    included in "unevaluated_partitioned_event_ids", because it's after the event that the cursor
    for its partition has advanced to, but trails "latest_evaluated_event_id".

    Attributes:
        latest_consumed_event_partition (Optional[str]): The partition of the latest consumed event
            for this asset.
        latest_consumed_event_id (Optional[int]): The event ID of the latest consumed event for
            this asset.
        trailing_unconsumed_partitioned_event_ids (Dict[str, int]): A mapping containing
            the partition key mapped to the latest unconsumed materialization event for this
            partition with an ID less than latest_consumed_event_id.
    """

    def __new__(
        cls,
        latest_consumed_event_partition,
        latest_consumed_event_id,
        trailing_unconsumed_partitioned_event_ids,
    ):
        return super(MultiAssetSensorAssetCursorComponent, cls).__new__(
            cls,
            latest_consumed_event_partition=check.opt_str_param(
                latest_consumed_event_partition, "latest_consumed_event_partition"
            ),
            latest_consumed_event_id=check.opt_int_param(
                latest_consumed_event_id, "latest_consumed_event_id"
            ),
            trailing_unconsumed_partitioned_event_ids=check.dict_param(
                trailing_unconsumed_partitioned_event_ids,
                "trailing_unconsumed_partitioned_event_ids",
                key_type=str,
                value_type=int,
            ),
        )


class MultiAssetSensorContextCursor:
    # Tracks the state of the cursor within the tick, created for utility purposes.
    # Must call MultiAssetSensorEvaluationContext._update_cursor_after_evaluation at end of tick
    # to serialize the cursor.
    def __init__(self, cursor: Optional[str], context: "MultiAssetSensorEvaluationContext"):
        loaded_cursor = json.loads(cursor) if cursor else {}
        self._cursor_component_by_asset_key: Dict[str, MultiAssetSensorAssetCursorComponent] = {}

        # The initial latest consumed event ID at the beginning of the tick
        self.initial_latest_consumed_event_ids_by_asset_key: Dict[str, Optional[int]] = {}

        for str_asset_key, cursor_list in loaded_cursor.items():
            if len(cursor_list) != 3:
                # In this case, the cursor object is not a multi asset sensor asset cursor
                # component. This cursor is maintained by the asset reconciliation sensor.
                break
            else:
                partition_key, event_id, trailing_unconsumed_partitioned_event_ids = cursor_list
                self._cursor_component_by_asset_key[
                    str_asset_key
                ] = MultiAssetSensorAssetCursorComponent(
                    latest_consumed_event_partition=partition_key,
                    latest_consumed_event_id=event_id,
                    trailing_unconsumed_partitioned_event_ids=trailing_unconsumed_partitioned_event_ids,
                )

                self.initial_latest_consumed_event_ids_by_asset_key[str_asset_key] = event_id

        check.dict_param(self._cursor_component_by_asset_key, "unpacked_cursor", key_type=str)
        self._context = context

    def get_cursor_for_asset(self, asset_key: AssetKey) -> MultiAssetSensorAssetCursorComponent:
        return self._cursor_component_by_asset_key.get(
            str(asset_key), MultiAssetSensorAssetCursorComponent(None, None, {})
        )

    def get_stringified_cursor(self) -> str:
        return json.dumps(self._cursor_component_by_asset_key)


@experimental
class MultiAssetSensorEvaluationContext(SensorEvaluationContext):
    """The context object available as the argument to the evaluation function of a
    :py:class:`dagster.MultiAssetSensorDefinition`.

    Users should not instantiate this object directly. To construct a
    `MultiAssetSensorEvaluationContext` for testing purposes, use :py:func:`dagster.
    build_multi_asset_sensor_context`.

    The `MultiAssetSensorEvaluationContext` contains a cursor object that tracks the state of
    consumed event logs for each monitored asset. For each asset, the cursor stores the storage ID
    of the latest materialization that has been marked as "consumed" (via a call to `advance_cursor`)
    in a `latest_consumed_event_id` field.

    For each monitored asset, the cursor will store the latest unconsumed event ID for up to 25
    partitions. Each event ID must be before the `latest_consumed_event_id` field for the asset.

    Events marked as consumed via `advance_cursor` will be returned in future ticks until they
    are marked as consumed.

    To update the cursor to the latest materialization and clear the unconsumed events, call
    `advance_all_cursors`.

    Attributes:
        asset_keys (Sequence[AssetKey]): The asset keys that the sensor is configured to monitor.
        repository_def (RepositoryDefinition): The repository that the sensor belongs to.
        instance_ref (Optional[InstanceRef]): The serialized instance configured to run the schedule
        cursor (Optional[str]): The cursor, passed back from the last sensor evaluation via
            the cursor attribute of SkipReason and RunRequest. Must be a dictionary of asset key
            strings to a stringified tuple of (latest_event_partition, latest_event_storage_id,
            trailing_unconsumed_partitioned_event_ids).
        last_completion_time (float): DEPRECATED The last time that the sensor was consumed (UTC).
        last_run_key (str): DEPRECATED The run key of the RunRequest most recently created by this
            sensor. Use the preferred `cursor` attribute instead.
        repository_name (Optional[str]): The name of the repository that the sensor belongs to.
        instance (Optional[DagsterInstance]): The deserialized instance can also be passed in
            directly (primarily useful in testing contexts).

    Example:

    .. code-block:: python

        from dagster import multi_asset_sensor, MultiAssetSensorEvaluationContext

        @multi_asset_sensor(asset_keys=[AssetKey("asset_1), AssetKey("asset_2)])
        def the_sensor(context: MultiAssetSensorEvaluationContext):
            ...

    """

    def __init__(
        self,
        instance_ref: Optional[InstanceRef],
        last_completion_time: Optional[float],
        last_run_key: Optional[str],
        cursor: Optional[str],
        repository_name: Optional[str],
        repository_def: "RepositoryDefinition",
        asset_selection: Optional[AssetSelection],
        asset_keys: Optional[Sequence[AssetKey]],
        instance: Optional[DagsterInstance] = None,
    ):
        from dagster._core.storage.event_log.base import EventLogRecord

        self._repository_def = repository_def
        if asset_selection is not None:
            repo_assets = self._repository_def._assets_defs_by_key.values()
            repo_source_assets = self._repository_def.source_assets_by_key.values()
            self._asset_keys = list(asset_selection.resolve([*repo_assets, *repo_source_assets]))
        elif asset_keys is not None:
            self._asset_keys = list(asset_keys)
        else:
            raise DagsterInvalidDefinitionError(
                "MultiAssetSensorDefinition requires one of asset_selection or asset_keys"
            )

        self._assets_by_key: Dict[AssetKey, Optional[AssetsDefinition]] = {}
        for asset_key in self._asset_keys:
            assets_def = (
                self._repository_def._assets_defs_by_key.get(  # pylint:disable=protected-access
                    asset_key
                )
            )
            self._assets_by_key[asset_key] = assets_def

        self._partitions_def_by_asset_key = {
            asset_key: asset_def.partitions_def
            for asset_key, asset_def in self._assets_by_key.items()
            if asset_def is not None
        }

        # Cursor object with utility methods for updating and retrieving cursor information.
        # At the end of each tick, must call update_cursor_after_evaluation to update the serialized
        # cursor.
        self._unpacked_cursor = MultiAssetSensorContextCursor(cursor, self)
        self._cursor_has_been_updated = False
        self._cursor_advance_state_mutation = MultiAssetSensorCursorAdvances()

        self._initial_unconsumed_events_by_id: Dict[int, EventLogRecord] = {}
        self._fetched_initial_unconsumed_events = False

        super(MultiAssetSensorEvaluationContext, self).__init__(
            instance_ref=instance_ref,
            last_completion_time=last_completion_time,
            last_run_key=last_run_key,
            cursor=cursor,
            repository_name=repository_name,
            instance=instance,
        )

    def _cache_initial_unconsumed_events(self) -> None:
        from dagster._core.events import DagsterEventType
        from dagster._core.storage.event_log.base import EventRecordsFilter

        # This method caches the initial unconsumed events for each asset key. To generate the
        # current unconsumed events, call get_trailing_unconsumed_events instead.
        if self._fetched_initial_unconsumed_events:
            return

        for asset_key in self._asset_keys:
            event_records = self.instance.get_event_records(
                EventRecordsFilter(
                    event_type=DagsterEventType.ASSET_MATERIALIZATION,
                    storage_ids=list(
                        self._get_cursor(
                            asset_key
                        ).trailing_unconsumed_partitioned_event_ids.values()
                    ),
                )
            )
            self._initial_unconsumed_events_by_id.update(
                {event_record.storage_id: event_record for event_record in event_records}
            )

        self._fetched_initial_unconsumed_events = True

    def _get_unconsumed_events_with_ids(self, event_ids: List[int]) -> Sequence["EventLogRecord"]:
        self._cache_initial_unconsumed_events()
        unconsumed_events = []
        for event_id in sorted(event_ids):
            event = self._initial_unconsumed_events_by_id.get(event_id)
            unconsumed_events.extend([event] if event else [])

        return unconsumed_events

    @public
    def get_trailing_unconsumed_events(self, asset_key: AssetKey) -> Sequence["EventLogRecord"]:
        """Fetches the unconsumed events for a given asset key. Returns only events
        before the latest consumed event ID for the given asset. To mark an event as consumed,
        pass the event to `advance_cursor`. Returns events in ascending order by storage ID.

        Args:
            asset_key (AssetKey): The asset key to get unconsumed events for.

        Returns:
            Sequence[EventLogRecord]: The unconsumed events for the given asset key.
        """
        check.inst_param(asset_key, "asset_key", AssetKey)

        return self._get_unconsumed_events_with_ids(
            list(self._get_cursor(asset_key).trailing_unconsumed_partitioned_event_ids.values())
        )

    def _get_partitions_after_cursor(self, asset_key: AssetKey) -> List[str]:
        asset_key = check.inst_param(asset_key, "asset_key", AssetKey)
        partition_key = self._get_cursor(asset_key).latest_consumed_event_partition

        partitions_def = self._partitions_def_by_asset_key.get(asset_key)

        if not isinstance(partitions_def, PartitionsDefinition):
            raise DagsterInvalidInvocationError(f"No partitions defined for asset key {asset_key}")

        partitions_to_fetch = list(partitions_def.get_partition_keys())

        if partition_key is not None:
            # Return partitions after the cursor partition, not including the cursor partition
            partitions_to_fetch = partitions_to_fetch[
                partitions_to_fetch.index(partition_key) + 1 :
            ]
        return partitions_to_fetch

    def update_cursor_after_evaluation(self) -> None:
        """Updates the cursor after the sensor evaluation function has been called. This method
        should be called at most once per evaluation.
        """

        new_cursor = self._cursor_advance_state_mutation.get_cursor_with_advances(
            self, self._unpacked_cursor
        )

        if new_cursor != None:
            # Cursor was not updated by this context object, so we do not need to update it
            self._cursor = new_cursor
            self._unpacked_cursor = MultiAssetSensorContextCursor(new_cursor, self)
            self._cursor_advance_state_mutation = MultiAssetSensorCursorAdvances()

    @public
    def latest_materialization_records_by_key(
        self,
        asset_keys: Optional[Sequence[AssetKey]] = None,
    ) -> Mapping[AssetKey, Optional["EventLogRecord"]]:
        """Fetches the most recent materialization event record for each asset in asset_keys.
        Only fetches events after the latest consumed event ID for the given asset key.

        Args:
            asset_keys (Optional[Sequence[AssetKey]]): list of asset keys to fetch events for. If
                not specified, the latest materialization will be fetched for all assets the
                multi_asset_sensor monitors.

        Returns: Mapping of AssetKey to EventLogRecord where the EventLogRecord is the latest
            materialization event for the asset. If there is no materialization event for the asset,
            the value in the mapping will be None.
        """
        from dagster._core.events import DagsterEventType
        from dagster._core.storage.event_log.base import EventRecordsFilter

        # Do not evaluate unconsumed events, only events newer than the cursor
        # if there are no new events after the cursor, the cursor points to the most
        # recent event.

        if asset_keys is None:
            asset_keys = self._asset_keys
        else:
            asset_keys = check.opt_list_param(asset_keys, "asset_keys", of_type=AssetKey)

        asset_event_records = {}
        for a in asset_keys:

            event_records = self.instance.get_event_records(
                EventRecordsFilter(
                    event_type=DagsterEventType.ASSET_MATERIALIZATION,
                    asset_key=a,
                    after_cursor=self._get_cursor(a).latest_consumed_event_id,
                ),
                ascending=False,
                limit=1,
            )

            if event_records:
                asset_event_records[a] = event_records[0]
            else:
                asset_event_records[a] = None

        return asset_event_records

    @public
    def materialization_records_for_key(
        self, asset_key: AssetKey, limit: int
    ) -> Iterable["EventLogRecord"]:
        """
        Fetches asset materialization event records for asset_key, with the earliest event first.

        Only fetches events after the latest consumed event ID for the given asset key.

        Args:
            asset_key (AssetKey): The asset to fetch materialization events for
            limit (int): The number of events to fetch
        """
        from dagster._core.events import DagsterEventType
        from dagster._core.storage.event_log.base import EventRecordsFilter

        asset_key = check.inst_param(asset_key, "asset_key", AssetKey)
        if asset_key not in self._assets_by_key:
            raise DagsterInvalidInvocationError(f"Asset key {asset_key} not monitored by sensor.")

        events = list(
            self.instance.get_event_records(
                EventRecordsFilter(
                    event_type=DagsterEventType.ASSET_MATERIALIZATION,
                    asset_key=asset_key,
                    after_cursor=self._get_cursor(asset_key).latest_consumed_event_id,
                ),
                ascending=True,
                limit=limit,
            )
        )

        return events

    def _get_cursor(self, asset_key: AssetKey) -> MultiAssetSensorAssetCursorComponent:
        """
        Returns the MultiAssetSensorAssetCursorComponent for the asset key.

        For more information, view the docstring for the MultiAssetSensorAssetCursorComponent class.
        """
        check.inst_param(asset_key, "asset_key", AssetKey)

        return self._unpacked_cursor.get_cursor_for_asset(asset_key)

    @public
    def latest_materialization_records_by_partition(
        self,
        asset_key: AssetKey,
        after_cursor_partition: Optional[bool] = False,
    ) -> Mapping[str, "EventLogRecord"]:
        """
        Given an asset, returns a mapping of partition key to the latest materialization event
        for that partition. Fetches only materializations that have not been marked as "consumed"
        via a call to `advance_cursor`.

        Args:
            asset_key (AssetKey): The asset to fetch events for.
            after_cursor_partition (Optional[bool]): If True, only materializations with partitions
                after the cursor's current partition will be returned. By default, set to False.

        Returns:
            Mapping[str, EventLogRecord]:
                Mapping of AssetKey to a mapping of partitions to EventLogRecords where the
                EventLogRecord is the most recent materialization event for the partition.
                The mapping preserves the order that the materializations occurred.

        Example:
            .. code-block:: python

                @asset(partitions_def=DailyPartitionsDefinition("2022-07-01"))
                def july_asset():
                    return 1

                @multi_asset_sensor(asset_keys=[july_asset.key])
                def my_sensor(context):
                    context.latest_materialization_records_by_partition(july_asset.key)

                # After materializing july_asset for 2022-07-05, latest_materialization_by_partition
                # returns {"2022-07-05": EventLogRecord(...)}

        """
        from dagster._core.events import DagsterEventType
        from dagster._core.storage.event_log.base import EventLogRecord, EventRecordsFilter

        asset_key = check.inst_param(asset_key, "asset_key", AssetKey)

        if asset_key not in self._assets_by_key:
            raise DagsterInvalidInvocationError(
                f"Asset key {asset_key} not monitored in sensor definition"
            )

        partitions_def = self._partitions_def_by_asset_key.get(asset_key)
        if not isinstance(partitions_def, PartitionsDefinition):
            raise DagsterInvariantViolationError(
                "Cannot get latest materialization by partition for assets with no partitions"
            )

        partitions_to_fetch = (
            self._get_partitions_after_cursor(asset_key)
            if after_cursor_partition
            else list(partitions_def.get_partition_keys())
        )

        # Retain ordering of materializations
        materialization_by_partition: Dict[str, EventLogRecord] = OrderedDict()

        # Add unconsumed events to the materialization by partition dictionary
        # These events came before the cursor, so should be inserted in storage ID ascending order
        for unconsumed_event in sorted(
            self._get_unconsumed_events_with_ids(
                list(self._get_cursor(asset_key).trailing_unconsumed_partitioned_event_ids.values())
            )
        ):
            partition = unconsumed_event.partition_key
            if isinstance(partition, str) and partition in partitions_to_fetch:
                if partition in materialization_by_partition:
                    # Remove partition to ensure materialization_by_partition preserves
                    # the order of materializations
                    materialization_by_partition.pop(partition)
                # Add partition and materialization to the end of the OrderedDict
                materialization_by_partition[partition] = unconsumed_event

        partition_materializations = self.instance.get_event_records(
            EventRecordsFilter(
                event_type=DagsterEventType.ASSET_MATERIALIZATION,
                asset_key=asset_key,
                asset_partitions=partitions_to_fetch,
                after_cursor=self._get_cursor(asset_key).latest_consumed_event_id,
            ),
            ascending=True,
        )
        for materialization in partition_materializations:
            partition = materialization.partition_key

            if isinstance(partition, str):
                if partition in materialization_by_partition:
                    # Remove partition to ensure materialization_by_partition preserves
                    # the order of materializations
                    materialization_by_partition.pop(partition)
                # Add partition and materialization to the end of the OrderedDict
                materialization_by_partition[partition] = materialization

        return materialization_by_partition

    @public
    def latest_materialization_records_by_partition_and_asset(
        self,
    ) -> Mapping[str, Mapping[AssetKey, "EventLogRecord"]]:
        """
        Finds the most recent unconsumed materialization for each partition for each asset
        monitored by the sensor. Aggregates all materializations into a mapping of partition key
        to a mapping of asset key to the materialization event for that partition.

        For example, if the sensor monitors two partitioned assets A and B that are materialized
        for partition_x after the cursor, this function returns:

            .. code-block:: python

                {
                    "partition_x": {asset_a.key: EventLogRecord(...), asset_b.key: EventLogRecord(...)}
                }

        This method can only be called when all monitored assets are partitioned and share
        the same partition definition.
        """
        partitions_defs = list(self._partitions_def_by_asset_key.values())
        if not partitions_defs or not all(x == partitions_defs[0] for x in partitions_defs):
            raise DagsterInvalidInvocationError(
                "All assets must be partitioned and share the same partitions definition"
            )

        asset_and_materialization_tuple_by_partition: Dict[
            str, Dict[AssetKey, "EventLogRecord"]
        ] = defaultdict(dict)

        for asset_key in self._asset_keys:
            materialization_by_partition = self.latest_materialization_records_by_partition(
                asset_key
            )
            for partition, materialization in materialization_by_partition.items():
                asset_and_materialization_tuple_by_partition[partition][asset_key] = materialization

        return asset_and_materialization_tuple_by_partition

    @public
    def get_cursor_partition(self, asset_key: Optional[AssetKey]) -> Optional[str]:
        """A utility method to get the current partition the cursor is on."""
        asset_key = check.opt_inst_param(asset_key, "asset_key", AssetKey)
        if asset_key not in self._asset_keys:
            raise DagsterInvalidInvocationError(
                "Provided asset key must correspond to a provided asset"
            )
        if asset_key:
            partition_key = self._get_cursor(asset_key).latest_consumed_event_partition
        elif self._asset_keys is not None and len(self._asset_keys) == 1:
            partition_key = self._get_cursor(self._asset_keys[0]).latest_consumed_event_partition
        else:
            raise DagsterInvalidInvocationError(
                "Asset key must be provided when multiple assets are defined"
            )

        return partition_key

    @public
    def all_partitions_materialized(
        self, asset_key: AssetKey, partitions: Optional[Sequence[str]] = None
    ) -> bool:
        """
        A utility method to check if a provided list of partitions have been materialized
        for a particular asset. This method ignores the cursor and checks all materializations
        for the asset.

        Args:
            asset_key (AssetKey): The asset to check partitions for.
            partitions (Optional[Sequence[str]]): A list of partitions to check. If not provided,
                all partitions for the asset will be checked.

        Returns:
            bool: True if all selected partitions have been materialized, False otherwise.
        """

        check.inst_param(asset_key, "asset_key", AssetKey)

        if partitions is not None:
            check.list_param(partitions, "partitions", of_type=str)
            if len(partitions) == 0:
                raise DagsterInvalidInvocationError("Must provide at least one partition in list")

        materialization_count_by_partition = self.instance.get_materialization_count_by_partition(
            [asset_key]
        ).get(asset_key, {})
        if not partitions:
            if asset_key not in self._asset_keys:
                raise DagsterInvariantViolationError(
                    f"Asset key {asset_key} not monitored by sensor"
                )

            partitions_def = self._partitions_def_by_asset_key.get(asset_key)
            if not partitions_def:
                raise DagsterInvariantViolationError(
                    f"Asset key {asset_key} is not partitioned. Cannot check if partitions have been materialized."
                )
            partitions = partitions_def.get_partition_keys()

        return all(
            [materialization_count_by_partition.get(partition, 0) != 0 for partition in partitions]
        )

    def _get_asset(self, asset_key: AssetKey, fn_name: str) -> AssetsDefinition:
        repository_assets = (
            self._repository_def._assets_defs_by_key  # pylint:disable=protected-access
        )
        if asset_key in self._assets_by_key:
            asset_def = self._assets_by_key[asset_key]
            if asset_def is None:
                raise DagsterInvalidInvocationError(
                    f"Asset key {asset_key} does not have an AssetDefinition in this repository (likely "
                    f"because it is a SourceAsset). fn context.{fn_name} can only be called for assets "
                    "with AssetDefinitions in the repository."
                )
            else:
                return asset_def
        elif asset_key in repository_assets:
            return repository_assets[asset_key]
        else:
            raise DagsterInvalidInvocationError(
                f"Asset key {asset_key} not monitored in sensor and does not exist in target jobs"
            )

    @public
    def get_downstream_partition_keys(
        self, partition_key: str, from_asset_key: AssetKey, to_asset_key: AssetKey
    ) -> Sequence[str]:
        """
        Converts a partition key from one asset to the corresponding partition key in a downstream
        asset. Uses the existing partition mapping between the upstream asset and the downstream
        asset if it exists, otherwise, uses the default partition mapping.

        Args:
            partition_key (str): The partition key to convert.
            from_asset_key (AssetKey): The asset key of the upstream asset, which the provided
                partition key belongs to.
            to_asset_key (AssetKey): The asset key of the downstream asset. The provided partition
                key will be mapped to partitions within this asset.

        Returns:
            Sequence[str]: A list of the corresponding downstream partitions in to_asset_key that
                partition_key maps to.
        """

        partition_key = check.str_param(partition_key, "partition_key")

        to_asset = self._get_asset(to_asset_key, fn_name="get_downstream_partition_keys")
        from_asset = self._get_asset(from_asset_key, fn_name="get_downstream_partition_keys")

        to_partitions_def = to_asset.partitions_def

        if not isinstance(to_partitions_def, PartitionsDefinition):
            raise DagsterInvalidInvocationError(
                f"Asset key {to_asset_key} is not partitioned. Cannot get partition keys."
            )
        if not isinstance(from_asset.partitions_def, PartitionsDefinition):
            raise DagsterInvalidInvocationError(
                f"Asset key {from_asset_key} is not partitioned. Cannot get partition keys."
            )

        partition_mapping = to_asset.get_partition_mapping(from_asset_key)
        downstream_partition_key_range = (
            partition_mapping.get_downstream_partitions_for_partition_range(
                PartitionKeyRange(partition_key, partition_key),
                downstream_partitions_def=to_partitions_def,
                upstream_partitions_def=from_asset.partitions_def,
            )
        )

        partition_keys = to_partitions_def.get_partition_keys()
        if (
            downstream_partition_key_range.start not in partition_keys
            or downstream_partition_key_range.end not in partition_keys
        ):
            error_msg = f"""Mapped partition key {partition_key} to downstream partition key range
            [{downstream_partition_key_range.start}...{downstream_partition_key_range.end}] which
            is not a valid range in the downstream partitions definition."""

            if not isinstance(to_partitions_def, TimeWindowPartitionsDefinition):
                raise DagsterInvalidInvocationError(error_msg)
            else:
                warnings.warn(error_msg)

        if isinstance(to_partitions_def, TimeWindowPartitionsDefinition):
            return to_partitions_def.get_partition_keys_in_range(downstream_partition_key_range)  # type: ignore[attr-defined]

        # Not a time-window partition definition
        downstream_partitions = partition_keys[
            partition_keys.index(downstream_partition_key_range.start) : partition_keys.index(
                downstream_partition_key_range.end
            )
            + 1
        ]
        return downstream_partitions

    @public
    def advance_cursor(
        self, materialization_records_by_key: Mapping[AssetKey, Optional["EventLogRecord"]]
    ):
        """
        Marks the provided materialization records as having been consumed by the sensor.

        At the end of the tick, the cursor will be updated to advance past all materializations
        records provided via `advance_cursor`. In the next tick, records that have been consumed
        will no longer be returned.

        Passing a partitioned materialization record into this function will mark prior materializations
        with the same asset key and partition as having been consumed.

        Args:
            materialization_records_by_key (Mapping[AssetKey, Optional[EventLogRecord]]): Mapping of
                AssetKeys to EventLogRecord or None. If an EventLogRecord is provided, the cursor
                for the AssetKey will be updated and future calls to fetch asset materialization events
                will not fetch this event again. If None is provided, the cursor for the AssetKey
                will not be updated.
        """

        self._cursor_advance_state_mutation.add_advanced_records(materialization_records_by_key)
        self._cursor_has_been_updated = True

    @public
    def advance_all_cursors(self):
        """
        Updates the cursor to the most recent materialization event for all assets monitored by
        the multi_asset_sensor.

        Marks all materialization events as consumed by the sensor, including unconsumed events.
        """
        materializations_by_key = self.latest_materialization_records_by_key()

        self._cursor_advance_state_mutation.add_advanced_records(materializations_by_key)
        self._cursor_advance_state_mutation.advance_all_cursors_called = True
        self._cursor_has_been_updated = True

    @public  # type: ignore
    @property
    def assets_defs_by_key(self) -> Mapping[AssetKey, Optional[AssetsDefinition]]:
        return self._assets_by_key

    @public  # type: ignore
    @property
    def asset_keys(self) -> Sequence[AssetKey]:
        return self._asset_keys


class MultiAssetSensorCursorAdvances:
    def __init__(self):
        self._advanced_record_ids_by_key: Dict[AssetKey, Set[int]] = defaultdict(set)
        self._partition_key_by_record_id: Dict[int, Optional[str]] = {}
        self.advance_all_cursors_called = False

    def add_advanced_records(
        self, materialization_records_by_key: Mapping[AssetKey, Optional["EventLogRecord"]]
    ):
        for asset_key, materialization in materialization_records_by_key.items():
            if materialization:
                self._advanced_record_ids_by_key[asset_key].add(materialization.storage_id)

                self._partition_key_by_record_id[
                    materialization.storage_id
                ] = materialization.partition_key

    def get_cursor_with_advances(
        self,
        context: MultiAssetSensorEvaluationContext,
        initial_cursor: MultiAssetSensorContextCursor,
    ) -> Optional[str]:
        """
        Given the multi asset sensor context and the cursor at the start of the tick,
        returns the cursor that should be used in the next tick.

        If the cursor has not been updated, returns None
        """
        if len(self._advanced_record_ids_by_key) == 0:
            # No events marked as advanced
            return None

        return json.dumps(
            {
                str(asset_key): self.get_asset_cursor_with_advances(
                    asset_key, context, initial_cursor
                )
                for asset_key in context.asset_keys
            }
        )

    def get_asset_cursor_with_advances(
        self,
        asset_key: AssetKey,
        context: MultiAssetSensorEvaluationContext,
        initial_cursor: MultiAssetSensorContextCursor,
    ) -> MultiAssetSensorAssetCursorComponent:
        from dagster._core.events import DagsterEventType
        from dagster._core.storage.event_log.base import EventRecordsFilter

        advanced_records: Set[int] = self._advanced_record_ids_by_key.get(asset_key, set())
        if len(advanced_records) == 0:
            # No events marked as advanced for this asset key
            return initial_cursor.get_cursor_for_asset(asset_key)

        initial_asset_cursor = initial_cursor.get_cursor_for_asset(asset_key)

        latest_consumed_event_id_at_tick_start = initial_asset_cursor.latest_consumed_event_id

        greatest_consumed_event_id_in_tick = max(advanced_records)
        latest_consumed_partition_in_tick = self._partition_key_by_record_id[
            greatest_consumed_event_id_in_tick
        ]
        latest_unconsumed_record_by_partition: Dict[str, int] = {}

        if not self.advance_all_cursors_called:
            latest_unconsumed_record_by_partition = (
                initial_asset_cursor.trailing_unconsumed_partitioned_event_ids
            )
            unconsumed_events = list(context.get_trailing_unconsumed_events(asset_key)) + list(
                context.instance.get_event_records(
                    EventRecordsFilter(
                        event_type=DagsterEventType.ASSET_MATERIALIZATION,
                        asset_key=asset_key,
                        after_cursor=latest_consumed_event_id_at_tick_start,
                        before_cursor=greatest_consumed_event_id_in_tick,
                    ),
                    ascending=True,
                )
                if greatest_consumed_event_id_in_tick
                > (latest_consumed_event_id_at_tick_start or 0)
                else []
            )

            # Iterate through events in ascending order, storing the latest unconsumed
            # event for each partition. If an advanced event exists for a partition, clear
            # the prior unconsumed event for that partition.
            for event in unconsumed_events:
                partition = event.partition_key
                if partition is not None:  # Ignore unpartitioned events
                    if not event.storage_id in advanced_records:
                        latest_unconsumed_record_by_partition[partition] = event.storage_id
                    elif partition in latest_unconsumed_record_by_partition:
                        latest_unconsumed_record_by_partition.pop(partition)

            if (
                latest_consumed_partition_in_tick is not None
                and latest_consumed_partition_in_tick in latest_unconsumed_record_by_partition
            ):
                latest_unconsumed_record_by_partition.pop(latest_consumed_partition_in_tick)

            if len(latest_unconsumed_record_by_partition.keys()) >= MAX_NUM_UNCONSUMED_EVENTS:
                raise DagsterInvariantViolationError(
                    f"""
                    You have reached the maximum number of trailing unconsumed events
                    ({MAX_NUM_UNCONSUMED_EVENTS}) for asset {asset_key} and no more events can be
                    added. You can access the unconsumed events by calling the
                    `get_trailing_unconsumed_events` method on the sensor context, and
                    mark events as consumed by passing them to `advance_cursor`.

                    Otherwise, you can clear all unconsumed events and reset the cursor to the latest
                    materialization for each asset by calling `advance_all_cursors`.
                    """
                )

        return MultiAssetSensorAssetCursorComponent(
            latest_consumed_event_partition=latest_consumed_partition_in_tick
            if greatest_consumed_event_id_in_tick > (latest_consumed_event_id_at_tick_start or 0)
            else initial_asset_cursor.latest_consumed_event_partition,
            latest_consumed_event_id=greatest_consumed_event_id_in_tick
            if greatest_consumed_event_id_in_tick > (latest_consumed_event_id_at_tick_start or 0)
            else latest_consumed_event_id_at_tick_start,
            trailing_unconsumed_partitioned_event_ids=latest_unconsumed_record_by_partition,
        )


def get_cursor_from_latest_materializations(
    asset_keys: Sequence[AssetKey], instance: DagsterInstance
) -> str:
    from dagster._core.events import DagsterEventType
    from dagster._core.storage.event_log.base import EventRecordsFilter

    cursor_dict: Dict[str, MultiAssetSensorAssetCursorComponent] = {}

    for asset_key in asset_keys:
        materializations = instance.get_event_records(
            EventRecordsFilter(
                DagsterEventType.ASSET_MATERIALIZATION,
                asset_key=asset_key,
            ),
            limit=1,
        )
        if materializations:
            last_materialization = list(materializations)[-1]

            cursor_dict[str(asset_key)] = MultiAssetSensorAssetCursorComponent(
                last_materialization.partition_key,
                last_materialization.storage_id,
                {},
            )

    cursor_str = json.dumps(cursor_dict)
    return cursor_str


@experimental
def build_multi_asset_sensor_context(
    repository_def: "RepositoryDefinition",
    asset_keys: Optional[Sequence[AssetKey]] = None,
    asset_selection: Optional[AssetSelection] = None,
    instance: Optional[DagsterInstance] = None,
    cursor: Optional[str] = None,
    repository_name: Optional[str] = None,
    cursor_from_latest_materializations: bool = False,
) -> MultiAssetSensorEvaluationContext:
    """Builds multi asset sensor execution context for testing purposes using the provided parameters.

    This function can be used to provide a context to the invocation of a multi asset sensor definition. If
    provided, the dagster instance must be persistent; DagsterInstance.ephemeral() will result in an
    error.

    Args:
        repository_def (RepositoryDefinition): The repository definition that the sensor belongs to.
        asset_keys (Optional[Sequence[AssetKey]]): The list of asset keys monitored by the sensor.
            If not provided, asset_selection argument must be provided.
        asset_selection (Optional[AssetSelection]): The asset selection monitored by the sensor.
            If not provided, asset_keys argument must be provided.
        instance (Optional[DagsterInstance]): The dagster instance configured to run the sensor.
        cursor (Optional[str]): A string cursor to provide to the evaluation of the sensor. Must be
            a dictionary of asset key strings to ints that has been converted to a json string
        repository_name (Optional[str]): The name of the repository that the sensor belongs to.
        cursor_from_latest_materializations (bool): If True, the cursor will be set to the latest
            materialization for each monitored asset. By default, set to False.

    Examples:

        .. code-block:: python

            with instance_for_test() as instance:
                context = build_multi_asset_sensor_context(asset_keys=[AssetKey("asset_1"), AssetKey("asset_2")], instance=instance)
                my_asset_sensor(context)

    """
    from dagster._core.definitions import RepositoryDefinition

    check.opt_inst_param(instance, "instance", DagsterInstance)
    check.opt_str_param(cursor, "cursor")
    check.opt_str_param(repository_name, "repository_name")
    check.inst_param(repository_def, "repository_def", RepositoryDefinition)
    check.invariant(asset_keys or asset_selection, "Must provide asset_keys or asset_selection")

    if asset_selection:
        asset_selection = check.inst_param(asset_selection, "asset_selection", AssetSelection)
        asset_keys = None
    else:  # asset keys provided
        asset_keys = check.opt_list_param(asset_keys, "asset_keys", of_type=AssetKey)
        check.invariant(len(asset_keys) > 0, "Must provide at least one asset key")
        asset_selection = None

    check.bool_param(cursor_from_latest_materializations, "cursor_from_latest_materializations")

    if cursor_from_latest_materializations:
        if cursor:
            raise DagsterInvalidInvocationError(
                "Cannot provide both cursor and cursor_from_latest_materializations objects. Dagster will override "
                "the provided cursor based on the cursor_from_latest_materializations object."
            )
        if not instance:
            raise DagsterInvalidInvocationError(
                "Cannot provide cursor_from_latest_materializations object without a Dagster instance."
            )

        if asset_selection:
            asset_keys = cast(
                List[AssetKey],
                list(
                    asset_selection.resolve(
                        list(
                            set(
                                repository_def._assets_defs_by_key.values()  # pylint: disable=protected-access
                            )
                        )
                    )
                ),
            )

        if asset_keys is None:
            asset_keys = []

        cursor = get_cursor_from_latest_materializations(asset_keys, instance)

    return MultiAssetSensorEvaluationContext(
        instance_ref=None,
        last_completion_time=None,
        last_run_key=None,
        cursor=cursor,
        repository_name=repository_name,
        instance=instance,
        asset_selection=asset_selection,
        asset_keys=asset_keys,
        repository_def=repository_def,
    )


AssetMaterializationFunctionReturn = Union[
    Iterator[Union[RunRequest, SkipReason]], Sequence[RunRequest], RunRequest, SkipReason, None
]
AssetMaterializationFunction = Callable[
    ["SensorEvaluationContext", "EventLogEntry"],
    AssetMaterializationFunctionReturn,
]

MultiAssetMaterializationFunction = Callable[
    ["MultiAssetSensorEvaluationContext"],
    AssetMaterializationFunctionReturn,
]


@experimental
class MultiAssetSensorDefinition(SensorDefinition):
    """Define an asset sensor that initiates a set of runs based on the materialization of a list of
    assets.

    Users should not instantiate this object directly. To construct a
    `MultiAssetSensorDefinition`, use :py:func:`dagster.
    multi_asset_sensor`.

    Args:
        name (str): The name of the sensor to create.
        asset_keys (Sequence[AssetKey]): The asset_keys this sensor monitors.
        asset_materialization_fn (Callable[[MultiAssetSensorEvaluationContext], Union[Iterator[Union[RunRequest, SkipReason]], RunRequest, SkipReason]]): The core
            evaluation function for the sensor, which is run at an interval to determine whether a
            run should be launched or not. Takes a :py:class:`~dagster.MultiAssetSensorEvaluationContext`.

            This function must return a generator, which must yield either a single SkipReason
            or one or more RunRequest objects.
        minimum_interval_seconds (Optional[int]): The minimum number of seconds that will elapse
            between sensor evaluations.
        description (Optional[str]): A human-readable description of the sensor.
        job (Optional[Union[GraphDefinition, JobDefinition, UnresolvedAssetJobDefinition]]): The job
            object to target with this sensor.
        jobs (Optional[Sequence[Union[GraphDefinition, JobDefinition, UnresolvedAssetJobDefinition]]]):
            (experimental) A list of jobs to be executed when the sensor fires.
        default_status (DefaultSensorStatus): Whether the sensor starts as running or not. The default
            status can be overridden from Dagit or via the GraphQL API.
    """

    def __init__(
        self,
        name: str,
        asset_keys: Optional[Sequence[AssetKey]],
        asset_selection: Optional[AssetSelection],
        job_name: Optional[str],
        asset_materialization_fn: Callable[
            ["MultiAssetSensorEvaluationContext"],
            RawSensorEvaluationFunctionReturn,
        ],
        minimum_interval_seconds: Optional[int] = None,
        description: Optional[str] = None,
        job: Optional[ExecutableDefinition] = None,
        jobs: Optional[Sequence[ExecutableDefinition]] = None,
        default_status: DefaultSensorStatus = DefaultSensorStatus.STOPPED,
    ):

        check.invariant(asset_keys or asset_selection, "Must provide asset_keys or asset_selection")
        self._asset_selection: Optional[AssetSelection] = None
        self._asset_keys: Optional[List[AssetKey]] = None
        if asset_selection:
            self._asset_selection = check.inst_param(
                asset_selection, "asset_selection", AssetSelection
            )
        else:  # asset keys provided
            asset_keys = check.opt_list_param(asset_keys, "asset_keys", of_type=AssetKey)
            self._asset_keys = asset_keys

        def _wrap_asset_fn(materialization_fn):
            def _fn(context):
                result = materialization_fn(context)
                if result is None:
                    return

                # because the materialization_fn can yield results (see _wrapped_fn in multi_asset_sensor decorator),
                # even if you return None in a sensor, it will still cause in inspect.isgenerator(result) to be True.
                # So keep track to see if we actually return any values and should update the cursor
                runs_yielded = False
                if inspect.isgenerator(result) or isinstance(result, list):
                    for item in result:
                        if isinstance(item, RunRequest):
                            runs_yielded = True
                        yield item
                elif isinstance(result, RunRequest):
                    runs_yielded = True
                    yield result
                elif isinstance(result, SkipReason):
                    # if result is a SkipReason, we don't update the cursor, so don't set runs_yielded = True
                    yield result

                if (
                    runs_yielded
                    and not context._cursor_has_been_updated  # pylint: disable=protected-access
                ):
                    raise DagsterInvalidDefinitionError(
                        "Asset materializations have been handled in this sensor, "
                        "but the cursor was not updated. This means the same materialization events "
                        "will be handled in the next sensor tick. Use context.advance_cursor or "
                        "context.advance_all_cursors to update the cursor."
                    )

                context.update_cursor_after_evaluation()

            return _fn

        super(MultiAssetSensorDefinition, self).__init__(
            name=check_valid_name(name),
            job_name=job_name,
            evaluation_fn=_wrap_asset_fn(
                check.callable_param(asset_materialization_fn, "asset_materialization_fn"),
            ),
            minimum_interval_seconds=minimum_interval_seconds,
            description=description,
            job=job,
            jobs=jobs,
            default_status=default_status,
        )

    def __call__(self, *args, **kwargs):

        if is_context_provided(self._raw_fn):
            if len(args) + len(kwargs) == 0:
                raise DagsterInvalidInvocationError(
                    "Sensor evaluation function expected context argument, but no context argument "
                    "was provided when invoking."
                )
            if len(args) + len(kwargs) > 1:
                raise DagsterInvalidInvocationError(
                    "Sensor invocation received multiple arguments. Only a first "
                    "positional context parameter should be provided when invoking."
                )

            context_param_name = get_function_params(self._raw_fn)[0].name

            if args:
                context = check.inst_param(
                    args[0], context_param_name, MultiAssetSensorEvaluationContext
                )
            else:
                if context_param_name not in kwargs:
                    raise DagsterInvalidInvocationError(
                        f"Sensor invocation expected argument '{context_param_name}'."
                    )
                context = check.inst_param(
                    kwargs[context_param_name],
                    context_param_name,
                    MultiAssetSensorEvaluationContext,
                )

            return self._raw_fn(context)

        else:
            if len(args) + len(kwargs) > 0:
                raise DagsterInvalidInvocationError(
                    "Sensor decorated function has no arguments, but arguments were provided to "
                    "invocation."
                )

            return self._raw_fn()  # type: ignore [TypeGuard limitation]

    @public  # type: ignore
    @property
    def asset_selection(self) -> Optional[AssetSelection]:
        return self._asset_selection

    @public  # type: ignore
    @property
    def asset_keys(self) -> Optional[Sequence[AssetKey]]:
        return self._asset_keys