from __future__ import annotations

import unittest

from master_list_creation.sources import parse_ann_images


class AnnSourceTest(unittest.TestCase):
    def test_parses_picture_info_without_duplicating_nested_img(self) -> None:
        xml = b"""<ann>
          <anime id="1">
            <info type="Picture" src="one.jpg" width="200" height="150">
              <img src="one.jpg" width="200" height="150" />
            </info>
            <info type="Picture" src="two.jpg" width="bad" height="200" />
            <info type="Genres">adventure</info>
          </anime>
        </ann>"""

        self.assertEqual(
            parse_ann_images(xml),
            {
                1: [
                    {"src": "one.jpg", "width": 200, "height": 150},
                    {"src": "two.jpg", "width": None, "height": 200},
                ]
            },
        )


if __name__ == "__main__":
    unittest.main()
