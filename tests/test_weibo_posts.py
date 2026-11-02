"""微博文案解析、短链提取与映射表读写。"""

from __future__ import annotations

import json

from astrbot_plugin_image_trace.weibo_posts import (
    WeiboImage,
    WeiboPost,
    dedupe_posts,
    extract_short_code,
    load_post_map,
    load_posts_from_dir,
    parse_weibo_csv,
    parse_weibo_md,
    save_post_map,
)

MD_SAMPLE = """# 微博原创文案 · 赵今麦angel（账号 2106192855）

- 范围：微博主页「原创」分类下的全部微博

## 2026 年（2 条）

1. 坠入秋日棕调氛围里🍂  
   @MiuMiu —— 2026-10-21 11:00 · 微博@赵今麦angel

2. 先锋无可替代  
   我有我的答案 —— 2026-10-19 10:10 · 微博@赵今麦angel

## 去掉话题和链接后无正文的微博（1 条）

- 2026-09-29 00:00 · 微博@赵今麦angel　[纯视频/直播分享]　微博ID 5348273390883792
"""

CSV_SAMPLE = """序号,发布时间,来源,文案,微博链接
1,"2026-10-21 11:00","微博@赵今麦angel","坠入秋日棕调氛围里🍂
@MiuMiu","https://weibo.com/2106192855/RnFxu0zgA"
2,"2026-10-19 10:10","微博@赵今麦angel","先锋无可替代
我有我的答案","https://weibo.com/2106192855/RnmlWjQgw"
"""


def test_parse_md_skips_appendix_and_reads_meta():
    posts = parse_weibo_md(MD_SAMPLE, "sample.md")
    assert len(posts) == 2
    first = posts[0]
    assert first.account == "赵今麦angel"
    assert first.published_at == "2026-10-21 11:00"
    assert first.date == "20261021"
    assert first.text == "坠入秋日棕调氛围里🍂\n@MiuMiu"
    assert first.link == ""


def test_parse_csv_and_dedupe_carries_link_to_md_post():
    md_posts = parse_weibo_md(MD_SAMPLE, "sample.md")
    csv_posts = parse_weibo_csv(CSV_SAMPLE, "sample.csv")
    assert len(csv_posts) == 2
    assert csv_posts[0].code == "RnFxu0zgA"

    merged = dedupe_posts(md_posts + csv_posts)
    first = next(post for post in merged if post.published_at == "2026-10-21 11:00")
    assert first.source == "sample.md"
    assert first.link == "https://weibo.com/2106192855/RnFxu0zgA"
    assert first.code == "RnFxu0zgA"


def test_extract_short_code_forms():
    assert extract_short_code("https://weibo.com/6670517617/Rp7r9Dbl5") == "Rp7r9Dbl5"
    assert extract_short_code("https://m.weibo.cn/detail/5359867919338383") == "5359867919338383"
    assert extract_short_code("Rp7r9Dbl5") == "Rp7r9Dbl5"
    assert extract_short_code("") == ""


def test_map_roundtrip(tmp_path):
    path = tmp_path / "weibo_post_map.json"
    posts = [
        WeiboPost(
            link="https://weibo.com/1/Aodpmwgks",
            account="赵今麦angel",
            published_at="2013-12-20 22:41",
            date="20131220",
            text="分享图片 我在:",
            code="Aodpmwgks",
            image=WeiboImage(
                file_name="【微博@赵今麦angel】20131220-01：发布会.jpg",
                image_url="https://img.example.com/file/a.jpg",
                thumb_url="https://img.example.com/file/thumb/a.jpg",
                score=1.0,
            ),
        ),
        WeiboPost(
            link="https://weibo.com/1/RnFxu0zgA",
            account="赵今麦angel",
            published_at="2026-10-21 11:00",
            date="20261021",
            text="坠入秋日棕调氛围里",
            code="RnFxu0zgA",
        ),
    ]
    save_post_map(str(path), posts, sources=["sample.csv"])
    loaded, generated_at, meta = load_post_map(str(path))
    assert generated_at > 0
    assert meta["count"] == 2
    assert meta["matched_count"] == 1
    assert loaded[0].image is not None
    assert loaded[1].image is None
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["items"][0]["code"] == "Aodpmwgks"


def test_load_post_map_tolerates_bad_field_types(tmp_path):
    """手工改坏映射表字段时降级，不让 /签到 与 /溯源状态 抛异常。"""
    path = tmp_path / "weibo_post_map.json"
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "generated_at": "oops",
                "count": "很多",
                "matched_count": "一条",
                "sources": "a.csv",
                "items": [
                    {
                        "account": "微博@赵今麦angel",
                        "published_at": "2026-10-19 10:10",
                        "text": "先锋无可替代",
                        "image": {
                            "image_url": "https://img.example.com/file/a.jpg",
                            "score": "很高",
                        },
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    posts, generated_at, meta = load_post_map(str(path))

    assert generated_at == 0.0
    assert len(posts) == 1
    assert posts[0].image is not None
    assert posts[0].image.score == 0.0
    assert posts[0].date == "20261019"
    assert meta["count"] == 1
    assert meta["matched_count"] == 1
    assert meta["sources"] == []


def test_load_posts_from_dir(tmp_path):
    (tmp_path / "a.csv").write_text(CSV_SAMPLE, encoding="utf-8")
    (tmp_path / "b.md").write_text(MD_SAMPLE, encoding="utf-8")
    posts, files, errors = load_posts_from_dir(str(tmp_path))
    assert len(posts) == 2
    assert len(files) == 2
    assert errors == []
    assert all(post.code for post in posts)
