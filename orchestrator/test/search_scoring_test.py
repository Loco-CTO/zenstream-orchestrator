import unittest

from app.search_scoring import match_score, normalize_search_text, search_grams


class SearchScoringTest(unittest.TestCase):
    def test_normalization_and_index_grams_are_shared(self):
        self.assertEqual(normalize_search_text("07-GHOST"), "07 ghost")
        grams = search_grams("Batman")
        self.assertIn("ba", grams)
        self.assertIn("bat", grams)
        self.assertIn("  b", grams)

    def test_exact_prefix_and_substring_scores_dominate_fuzzy_scores(self):
        exact = match_score("batman", "Batman")
        prefix = match_score("batman", "Batman Begins")
        substring = match_score("batman", "The Batman")
        fuzzy = match_score("batmn", "Batman")
        self.assertEqual(exact, 1.0)
        self.assertEqual(prefix, 0.99)
        self.assertEqual(substring, 0.98)
        self.assertGreater(fuzzy, 0.0)
        self.assertLess(fuzzy, substring)

    def test_word_span_scoring_handles_multitoken_typos(self):
        self.assertGreater(match_score("spder man", "Spider-Man: No Way Home"), 0.0)
        self.assertGreater(match_score("gintma", "Gintama - Mr. Ginpachi's Zany Class"), 0.0)


if __name__ == "__main__":
    unittest.main()
