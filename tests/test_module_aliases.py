import unittest


class ModuleAliasTests(unittest.TestCase):
    def test_gui_module_alias_exposes_main(self):
        import yt_asr

        self.assertTrue(callable(yt_asr.main))

    def test_dataset_module_alias_exposes_main(self):
        import yt_asr_dataset

        self.assertTrue(callable(yt_asr_dataset.main))


if __name__ == "__main__":
    unittest.main()
