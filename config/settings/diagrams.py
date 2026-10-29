"""Settings for `make diagrams`: the test settings plus django-extensions' graph_models."""

from .test import *  # noqa: F403
from .test import INSTALLED_APPS

INSTALLED_APPS = INSTALLED_APPS + ["django_extensions"]
