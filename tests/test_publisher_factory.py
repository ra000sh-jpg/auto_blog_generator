from modules.uploaders.naver_publisher import NaverPublisher
from modules.uploaders.publisher_factory import extract_blog_id, get_publisher
from modules.uploaders.tistory_publisher import TistoryPublisher


def test_extract_blog_id_from_url() -> None:
    assert extract_blog_id("https://blog.naver.com/sample_blog") == "sample_blog"
    assert extract_blog_id("blog.naver.com/sample_blog") == "sample_blog"


def test_get_publisher_returns_naver_publisher() -> None:
    channel = {
        "channel_id": "channel-1",
        "platform": "naver",
        "blog_url": "https://blog.naver.com/sample_blog",
        "auth_json": '{"session_dir":"data/sessions/naver_sub1"}',
    }

    publisher = get_publisher(channel)

    assert isinstance(publisher, NaverPublisher)
    assert publisher.blog_id == "sample_blog"
    assert publisher.session_dir == "data/sessions/naver_sub1"


def test_get_publisher_returns_tistory_publisher() -> None:
    channel = {
        "channel_id": "channel-2",
        "platform": "tistory",
        "blog_url": "https://my-blog.tistory.com",
        "auth_json": '{"access_token":"token-value","blog_name":"my-blog"}',
    }

    publisher = get_publisher(channel)

    assert isinstance(publisher, TistoryPublisher)
    assert publisher.blog_name == "my-blog"
    assert publisher.access_token == "token-value"
