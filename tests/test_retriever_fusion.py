"""RRF 融合分数归一化：参考来源的"相关度"全通道同尺（0~1），不再向量/BM25 量纲混排。"""
import unittest

from src.retriever import RetrievedItem, _rrf_fuse


def _item(rank, score, chunk_id, channels):
    return RetrievedItem(rank=rank, score=score, text=f"文本{rank}",
                         metadata={"chunk_id": chunk_id, "source": f"s{rank}"},
                         channels=channels)


class RrfFuseNormalizationTests(unittest.TestCase):
    def test_scores_normalized_to_zero_one_regardless_of_channel(self):
        """BM25 原始分 9.9 的关键词来源，归一化后同样落回 0~1。"""
        vector = [_item(1, 0.91, 'c1', '向量'), _item(2, 0.50, 'c2', '向量')]
        keyword = [_item(1, 9.90, 'c1', '关键词'), _item(2, 7.30, 'c3', '关键词')]
        out, _ = _rrf_fuse(vector, keyword, top_k=3)
        # c2/c3 均为单通道第 2 名（1/62），平名次按插入序稳定排序
        self.assertEqual([i.metadata['chunk_id'] for i in out], ['c1', 'c2', 'c3'])
        for item in out:
            self.assertGreaterEqual(item.score, 0.0)
            self.assertLessEqual(item.score, 1.0)
        # 双通道同命中（c1）融合分最高；归一化锚点：双通道同名次 = 1.0
        self.assertAlmostEqual(out[0].score, 1.0, places=6)
        self.assertAlmostEqual(out[1].score, out[2].score, places=6)

    def test_single_channel_hits_stay_in_zero_one(self):
        vector = [_item(1, 0.83, 'c1', '向量')]
        out, _ = _rrf_fuse(vector, [], top_k=1)
        self.assertAlmostEqual(out[0].score, 0.5, places=6)  # 单通道第 1 名 = 1/61 ÷ (2/61)

    def test_ranking_order_is_preserved(self):
        vector = [_item(1, 0.40, 'a', '向量'), _item(2, 0.35, 'b', '向量'),
                  _item(3, 0.30, 'c', '向量')]
        keyword = [_item(1, 8.00, 'a', '关键词'), _item(2, 6.00, 'b', '关键词'),
                   _item(3, 4.00, 'c', '关键词')]
        out, _ = _rrf_fuse(vector, keyword, top_k=3)
        self.assertEqual([i.metadata['chunk_id'] for i in out], ['a', 'b', 'c'])
        self.assertEqual([round(i.score, 4) for i in out], [1.0, 0.9839, 0.9683])


if __name__ == "__main__":
    unittest.main()
