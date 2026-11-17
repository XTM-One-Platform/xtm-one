# Copyright (c) 2025-2026 Filigran SAS
#
# This file is part of the XTM One Enterprise Edition ("EE") and is
# licensed under the XTM One Enterprise Edition License (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://github.com/XTM-One-Platform/xtm-one/blob/main/LICENSE
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.

"""Exceptions for the evaluation metrics package.

Raised for invalid caller configuration and violated internal contracts; never converted
into a metric score.
"""


class EvaluationMetricsError(Exception):
    """Base class for all package-specific exceptions."""


class ConfigurationError(EvaluationMetricsError):
    """Invalid caller configuration.

    Configuration that cannot produce a meaningful score: a missing schema, vocabulary or
    canonicalizer, a reference/prediction length mismatch, an unknown comparison-policy option.
    """


class InternalContractError(EvaluationMetricsError):
    """A violated programmer contract or internal defect.

    A bug in the package rather than bad caller input; raised, never masked as ``invalid``.
    """


__all__ = [
    "ConfigurationError",
    "EvaluationMetricsError",
    "InternalContractError",
]
