# -------------------------------------------------------------------------
# Copyright (c) Microsoft Corporation. All rights reserved.
# Licensed under the MIT License. See License in the project root for
# license information.
# --------------------------------------------------------------------------
from logging import getLogger
from typing import Dict, cast

from opentelemetry._logs import get_logger_provider, set_logger_provider
from opentelemetry.instrumentation.dependencies import (
    get_dist_dependency_conflicts,
)
from opentelemetry.instrumentation.instrumentor import ( # type: ignore
    BaseInstrumentor,
)
from opentelemetry.metrics import set_meter_provider
from opentelemetry.sdk._logs import LoggerProvider, LoggingHandler
from opentelemetry.sdk._logs.export import BatchLogRecordProcessor
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.trace import get_tracer_provider, set_tracer_provider
from pkg_resources import iter_entry_points  # type: ignore

from azure.core.settings import settings
from azure.core.tracing.ext.opentelemetry_span import OpenTelemetrySpan
from azure.monitor.opentelemetry._constants import (
    _ALL_SUPPORTED_INSTRUMENTED_LIBRARIES,
    _AZURE_SDK_INSTRUMENTATION_NAME,
    DISABLE_LOGGING_ARG,
    DISABLE_METRICS_ARG,
    DISABLE_TRACING_ARG,
    LOGGER_NAME_ARG,
    RESOURCE_ARG,
    SAMPLING_RATIO_ARG,
)
from azure.monitor.opentelemetry._types import ConfigurationValue
from azure.monitor.opentelemetry.exporter import (  # pylint: disable=import-error,no-name-in-module
    ApplicationInsightsSampler,
    AzureMonitorLogExporter,
    AzureMonitorMetricExporter,
    AzureMonitorTraceExporter,
)
from azure.monitor.opentelemetry._util.configurations import (
    _get_configurations,
    _is_instrumentation_enabled,
)

_logger = getLogger(__name__)


def configure_azure_monitor(**kwargs) -> None:
    """This function works as a configuration layer that allows the
    end user to configure OpenTelemetry and Azure monitor components. The
    configuration can be done via arguments passed to this function.

    :keyword str connection_string: Connection string for your Application Insights resource.
    :keyword credential: Token credential, such as `ManagedIdentityCredential` or `ClientSecretCredential`,
     used for Azure Active Directory (AAD) authentication. Defaults to `None`.
    :paramtype credential: ~azure.core.credentials.TokenCredential or None
    :keyword bool disable_offline_storage: Boolean value to determine whether to disable storing failed
     telemetry records for retry. Defaults to `False`.
    :keyword str storage_directory: Storage directory in which to store retry files. Defaults to
     `<tempfile.gettempdir()>/Microsoft/AzureMonitor/opentelemetry-python-<your-instrumentation-key>`.
    :keyword str logger_name: The name of the Python logger that telemetry will be collected.
    :rtype: None
    """

    configurations = _get_configurations(**kwargs)

    disable_tracing = configurations[DISABLE_TRACING_ARG]
    disable_logging = configurations[DISABLE_LOGGING_ARG]
    disable_metrics = configurations[DISABLE_METRICS_ARG]

    # Setup tracing pipeline
    if not disable_tracing:
        _setup_tracing(configurations)

    # Setup logging pipeline
    if not disable_logging:
        _setup_logging(configurations)

    # Setup metrics pipeline
    if not disable_metrics:
        _setup_metrics(configurations)

    # Setup instrumentations
    # Instrumentations need to be setup last so to use the global providers
    # instanstiated in the other setup steps
    _setup_instrumentations(configurations)


def _setup_tracing(configurations: Dict[str, ConfigurationValue]):
    resource: Resource = configurations[RESOURCE_ARG] # type: ignore
    sampling_ratio = configurations[SAMPLING_RATIO_ARG]
    tracer_provider = TracerProvider(
        sampler=ApplicationInsightsSampler(sampling_ratio=cast(float, sampling_ratio)),
        resource=resource
    )
    set_tracer_provider(tracer_provider)
    trace_exporter = AzureMonitorTraceExporter(**configurations)
    span_processor = BatchSpanProcessor(
        trace_exporter,
    )
    get_tracer_provider().add_span_processor(span_processor) # type: ignore
    if _is_instrumentation_enabled(configurations, _AZURE_SDK_INSTRUMENTATION_NAME):
        settings.tracing_implementation = OpenTelemetrySpan


def _setup_logging(configurations: Dict[str, ConfigurationValue]):
    resource: Resource = configurations[RESOURCE_ARG] # type: ignore
    logger_provider = LoggerProvider(resource=resource)
    set_logger_provider(logger_provider)
    log_exporter = AzureMonitorLogExporter(**configurations)
    log_record_processor = BatchLogRecordProcessor(
        log_exporter,
    )
    get_logger_provider().add_log_record_processor(log_record_processor) # type: ignore
    handler = LoggingHandler(logger_provider=get_logger_provider())
    logger_name: str = configurations[LOGGER_NAME_ARG] # type: ignore
    getLogger(logger_name).addHandler(handler)


def _setup_metrics(configurations: Dict[str, ConfigurationValue]):
    resource: Resource = configurations[RESOURCE_ARG] # type: ignore
    metric_exporter = AzureMonitorMetricExporter(**configurations)
    reader = PeriodicExportingMetricReader(metric_exporter)
    meter_provider = MeterProvider(
        metric_readers=[reader],
        resource=resource
    )
    set_meter_provider(meter_provider)


def _setup_instrumentations(configurations: Dict[str, ConfigurationValue]):
    # use pkg_resources for now until https://github.com/open-telemetry/opentelemetry-python/pull/3168 is merged
    for entry_point in iter_entry_points(
        "opentelemetry_instrumentor"
    ):
        lib_name = entry_point.name
        if lib_name not in _ALL_SUPPORTED_INSTRUMENTED_LIBRARIES:
            continue
        if not _is_instrumentation_enabled(configurations, lib_name):
            _logger.debug(
                "Instrumentation skipped for library %s", entry_point.name
            )
            continue
        try:
            # Check if dependent libraries/version are installed
            conflict = get_dist_dependency_conflicts(entry_point.dist)
            if conflict:
                _logger.debug(
                    "Skipping instrumentation %s: %s",
                    entry_point.name,
                    conflict,
                )
                continue
            # Load the instrumentor via entrypoint
            instrumentor: BaseInstrumentor = entry_point.load()
            # tell instrumentation to not run dep checks again as we already did it above
            instrumentor().instrument(skip_dep_check=True)
        except Exception as ex:  # pylint: disable=broad-except
            _logger.warning(
                "Exception occurred when instrumenting: %s.",
                lib_name,
                exc_info=ex,
            )
