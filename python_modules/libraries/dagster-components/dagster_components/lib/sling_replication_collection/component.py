from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Callable, Optional, Union

from dagster._core.definitions.assets import AssetsDefinition
from dagster._core.definitions.definitions_class import Definitions
from dagster._core.definitions.events import AssetMaterialization
from dagster._core.definitions.result import MaterializeResult
from dagster_sling import DagsterSlingTranslator, SlingResource, sling_assets
from dagster_sling.resources import AssetExecutionContext
from pydantic import BaseModel

from dagster_components import Component, ComponentLoadContext, FieldResolver
from dagster_components.core.component import registered_component_type
from dagster_components.core.component_scaffolder import ComponentScaffolder
from dagster_components.core.schema.base import ResolvableSchema
from dagster_components.core.schema.context import ResolutionContext
from dagster_components.core.schema.metadata import ResolvableFieldInfo
from dagster_components.core.schema.objects import (
    AssetAttributesSchema,
    AssetSpecTransformSchema,
    OpSpecSchema,
)
from dagster_components.utils import TranslatorResolvingInfo, get_wrapped_translator_class


def resolve_translator(
    context: ResolutionContext, schema: "SlingReplicationSchema"
) -> DagsterSlingTranslator:
    return get_wrapped_translator_class(DagsterSlingTranslator)(
        resolving_info=TranslatorResolvingInfo(
            "stream_definition",
            schema.asset_attributes or AssetAttributesSchema(),
            context,
        )
    )


class SlingReplicationSpec(BaseModel):
    path: str
    op: Optional[OpSpecSchema]
    translator: Annotated[Optional[DagsterSlingTranslator], FieldResolver(resolve_translator)]


class SlingReplicationSchema(ResolvableSchema[SlingReplicationSpec]):
    path: str
    op: Optional[OpSpecSchema] = None
    asset_attributes: Annotated[
        Optional[AssetAttributesSchema],
        ResolvableFieldInfo(required_scope={"stream_definition"}),
    ] = None


class SlingReplicationCollectionSchema(ResolvableSchema["SlingReplicationCollectionSchema"]):
    sling: Optional[SlingResource] = None
    replications: Sequence[SlingReplicationSchema]
    transforms: Optional[Sequence[AssetSpecTransformSchema]] = None


def resolve_resource(
    context: ResolutionContext, schema: SlingReplicationCollectionSchema
) -> SlingResource:
    return (
        SlingResource(**context.resolve_value(schema.sling.model_dump()))
        if schema.sling
        else SlingResource()
    )


@registered_component_type
@dataclass
class SlingReplicationCollection(Component):
    """Expose one or more Sling replications to Dagster as assets."""

    resource: Annotated[SlingResource, FieldResolver(resolve_resource)]
    replications: Sequence[SlingReplicationSpec]
    transforms: Optional[Sequence[Callable[[Definitions], Definitions]]]

    @classmethod
    def get_scaffolder(cls) -> ComponentScaffolder:
        from dagster_components.lib.sling_replication_collection.scaffolder import (
            SlingReplicationComponentScaffolder,
        )

        return SlingReplicationComponentScaffolder()

    @classmethod
    def get_schema(cls) -> type[SlingReplicationCollectionSchema]:
        return SlingReplicationCollectionSchema

    def build_asset(
        self, context: ComponentLoadContext, replication_spec: SlingReplicationSpec
    ) -> AssetsDefinition:
        op_spec = replication_spec.op or OpSpecSchema()

        @sling_assets(
            name=op_spec.name or Path(replication_spec.path).stem,
            op_tags=op_spec.tags,
            replication_config=context.path / replication_spec.path,
            dagster_sling_translator=replication_spec.translator,
        )
        def _asset(context: AssetExecutionContext):
            yield from self.execute(context=context, sling=self.resource)

        return _asset

    def execute(
        self, context: AssetExecutionContext, sling: SlingResource
    ) -> Iterator[Union[AssetMaterialization, MaterializeResult]]:
        yield from sling.replicate(context=context)

    def build_defs(self, context: ComponentLoadContext) -> Definitions:
        defs = Definitions(
            assets=[self.build_asset(context, replication) for replication in self.replications],
        )
        for transform in self.transforms or []:
            defs = transform(defs)
        return defs
