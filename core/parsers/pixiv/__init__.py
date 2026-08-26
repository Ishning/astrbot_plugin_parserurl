from typing import ClassVar

from ..base import BaseParser
from ...data import Platform

class PixivParser(BaseParser):
    platform: ClassVar[Platform] = Platform(name="pixiv", display_name="Pixiv")

