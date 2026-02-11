# Copyright (C) 2026 Famedly
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with this program. If not, see <http://www.gnu.org/licenses/>.
import logging
from typing import Any

from famedly_control_synapse.config import FamedlyControlConfig

logger = logging.getLogger(__name__)


class FamedlyControl:
    __version__ = "0.0.1"

    def __init__(self, config: FamedlyControlConfig):
        self.config = config

        logger.info("Module initialized")

    @staticmethod
    def parse_config(config: dict[str, Any]) -> FamedlyControlConfig:
        return FamedlyControlConfig.model_validate(config)
