"""Static, non-secret configuration for the supported albums."""

from dataclasses import dataclass
from typing import Tuple


@dataclass(frozen=True)
class AlbumConfig:
    key: str
    title: str
    album_id: str
    output_directory: str

    @property
    def page_url(self) -> str:
        return "https://afdian.com/album/{}".format(self.album_id)


DEFAULT_ALBUMS: Tuple[AlbumConfig, ...] = (
    AlbumConfig(
        key="ten-hole",
        title="十孔口琴",
        album_id="11beb000f14f11ec800352540025c377",
        output_directory="十孔口琴",
    ),
    AlbumConfig(
        key="chromatic",
        title="半音阶口琴",
        album_id="5dc07fbaf1cc11ec896052540025c377",
        output_directory="半音阶口琴",
    ),
    AlbumConfig(
        key="whistle",
        title="哨笛",
        album_id="73f44a40b65411eca6d752540025c377",
        output_directory="哨笛",
    ),
)

