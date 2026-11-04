"""Unit: offline threshold evaluation and recommendation for calibration."""

from app.services.retrieval_eval import CaseRun, evaluate, recommend


def _runs():
    return [
        # positives: expected item A
        CaseRun("买错了能退吗", "A", candidates=[("A", "退款政策", 0.17), ("B", "QQ群", 0.16)]),
        CaseRun("上传文件失败", "C", candidates=[("C", "上传文件报错", 0.90)]),
        CaseRun("claude code 怎么接入", "D", candidates=[("D", "API中转站", 0.167)]),
        # negatives: nothing should come back
        CaseRun("订单号在哪找", None, candidates=[("B", "QQ群", 0.188)]),
        CaseRun("今天天气怎么样", None, candidates=[]),
        CaseRun("模型广场都有什么", None, candidates=[("B", "QQ群", 0.159)]),
    ]


def test_evaluate_counts_hits_and_rejections_per_threshold():
    r15 = evaluate(_runs(), 0.15, top_k=5)
    assert (r15["hits"], r15["positives"], r15["rejects"], r15["negatives"]) == (3, 3, 1, 3)
    assert r15["accuracy"] == round(4 / 6, 3)
    r20 = evaluate(_runs(), 0.20, top_k=5)
    assert (r20["hits"], r20["rejects"]) == (1, 3)  # raising the floor loses two true hits
    r175 = evaluate(_runs(), 0.175, top_k=5)
    assert (r175["hits"], r175["rejects"]) == (1, 2)  # only the 0.90 case survives


def test_evaluate_respects_top_k():
    runs = [CaseRun("q", "Z", candidates=[("A", "", 0.9), ("B", "", 0.8), ("Z", "", 0.7)])]
    assert evaluate(runs, 0.0, top_k=2)["hits"] == 0
    assert evaluate(runs, 0.0, top_k=3)["hits"] == 1


def test_recommend_prefers_accuracy_then_closeness_to_current():
    rows = [
        {"value": 0.10, "accuracy": 0.5},
        {"value": 0.15, "accuracy": 0.67},
        {"value": 0.20, "accuracy": 0.67},
        {"value": 0.30, "accuracy": 0.6},
    ]
    assert recommend(rows, current=0.15) == 0.15
    assert recommend(rows, current=0.25) == 0.20
    assert recommend([], current=0.15) is None
    assert recommend([{"value": 0.1, "accuracy": None}], current=0.15) is None
