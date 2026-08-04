import unittest

from skills._shared.meta_extract import _parse_tag_lines


class MetaExtractTests(unittest.TestCase):
    def test_parses_ftimage_description_compact_tag_and_value(self):
        rows = _parse_tag_lines(
            "Patient Name(x00100010): Tang Yuan Hua\n"
            "Series Description(x0008103e): 1.5 x 1.5_lung\n"
            "Pixel Spacing(x00280030): 0.8203125\\0.8203125"
        )

        self.assertEqual(
            rows,
            [
                {
                    "tag": "(0010,0010)",
                    "desc": "Patient Name",
                    "value": "Tang Yuan Hua",
                },
                {
                    "tag": "(0008,103E)",
                    "desc": "Series Description",
                    "value": "1.5 x 1.5_lung",
                },
                {
                    "tag": "(0028,0030)",
                    "desc": "Pixel Spacing",
                    "value": "0.8203125\\0.8203125",
                },
            ],
        )

    def test_keeps_standard_tag_first_table_text_compatible(self):
        rows = _parse_tag_lines(
            "(0010,0010)\tPatient Name\tTang Yuan Hua\n"
            "(0010,0020)\tPatient ID\t0003699549"
        )

        self.assertEqual(rows[0]["desc"], "Patient Name")
        self.assertEqual(rows[0]["value"], "Tang Yuan Hua")
        self.assertEqual(rows[1]["desc"], "Patient ID")
        self.assertEqual(rows[1]["value"], "0003699549")

    def test_splits_multiple_ftimage_tags_concatenated_on_one_dom_line(self):
        rows = _parse_tag_lines(
            "Patient Name(x00100010): Tang Yuan Hua"
            "Patient ID(x00100020): 0003699549"
            "Patient Birth Date(x00100030): 19640320"
            "Patient Sex(x00100040): M"
        )

        self.assertEqual(
            [(row["tag"], row["desc"], row["value"]) for row in rows],
            [
                ("(0010,0010)", "Patient Name", "Tang Yuan Hua"),
                ("(0010,0020)", "Patient ID", "0003699549"),
                ("(0010,0030)", "Patient Birth Date", "19640320"),
                ("(0010,0040)", "Patient Sex", "M"),
            ],
        )

    def test_splits_ftimage_equipment_and_uid_fields(self):
        rows = _parse_tag_lines(
            "AE Title(x00020016): KSDEYYPACS"
            "Institution Name(x00080080): 喀什地区第二人民医院\n"
            "Study UID(x0020000d): 1.2.3.4"
            "Series UID(x0020000e): 1.2.3.4.5"
            "Instance UID(x00080018): 1.2.3.4.5.6"
        )

        self.assertEqual(
            [(row["desc"], row["value"]) for row in rows],
            [
                ("AE Title", "KSDEYYPACS"),
                ("Institution Name", "喀什地区第二人民医院"),
                ("Study UID", "1.2.3.4"),
                ("Series UID", "1.2.3.4.5"),
                ("Instance UID", "1.2.3.4.5.6"),
            ],
        )


if __name__ == "__main__":
    unittest.main()
