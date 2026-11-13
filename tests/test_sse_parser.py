"""SSE 字节解析器单元测试：重点是字节/字符/行边界。"""

import unittest

from collector.sse import SSEParser


def parse_all(chunks: list[bytes]) -> list:
    p = SSEParser()
    events = []
    for chunk in chunks:
        events.extend(p.feed(chunk))
    events.extend(p.end_stream())
    return events


class SSEParserTests(unittest.TestCase):
    def test_basic_event_with_id_event_and_data(self):
        raw = b"event: reading\r\nid: streamA:1\r\ndata: {\"a\":1}\r\n\r\n"
        (ev,) = parse_all([raw])
        self.assertEqual(ev.event, "reading")
        self.assertEqual(ev.event_id, "streamA:1")
        self.assertEqual(ev.data, '{"a":1}')

    def test_multiple_data_lines_joined_with_lf(self):
        raw = b"data: line1\ndata: line2\ndata: line3\n\n"
        (ev,) = parse_all([raw])
        self.assertEqual(ev.data, "line1\nline2\nline3")

    def test_comment_and_retry_only_blocks_emit_nothing(self):
        raw = b": heartbeat\r\nretry: 30\r\n\r\n"
        self.assertEqual(parse_all([raw]), [])
        p = SSEParser()
        p.feed(raw)
        self.assertEqual(p.retry_ms, 30)

    def test_comment_between_fields_is_ignored(self):
        raw = b"id: x:1\n: comment\ndata: hi\n\n"
        (ev,) = parse_all([raw])
        self.assertEqual(ev.event_id, "x:1")
        self.assertEqual(ev.data, "hi")

    def test_utf8_multibyte_split_across_chunks(self):
        text = "改写-正常-€𝄞"
        raw = ("data: " + text + "\n\n").encode("utf-8")
        # 按 1 字节喂入，必然把多字节字符切碎。
        events = parse_all([raw[i:i + 1] for i in range(len(raw))])
        self.assertEqual(events[0].data, text)

    def test_crlf_split_across_chunks_no_blank_event(self):
        raw = b"data: a\r\ndata: b\r\n\r\n"
        # 在 \r 与 \n 之间切开。
        cut = raw.index(b"\r\n\r\n") + 1
        events = parse_all([raw[:cut], raw[cut:]])
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].data, "a\nb")

    def test_lone_cr_and_lf_line_endings(self):
        raw = b"data: a\rdata: b\n\r\ndata: c\n\n"
        events = parse_all([raw])
        self.assertEqual([e.data for e in events], ["a\nb", "c"])

    def test_event_dispatched_only_on_blank_line(self):
        p = SSEParser()
        self.assertEqual(p.feed(b"id: s:1\ndata: {}\n"), [])
        # 还没收到空行：没有事件。
        self.assertEqual(p.feed(b"\n")[0].event_id, "s:1")

    def test_half_message_at_eof_is_not_dispatched(self):
        # 上游断在半个 JSON、半个多字节字符上，且没有块结束空行。
        raw = b"event: reading\r\nid: streamA:4\r\ndata: {\"stream_id\": \"streamA\", \"value\": \"\xe6\xad\xa3"
        self.assertEqual(parse_all([raw]), [])

    def test_invalid_utf8_raises(self):
        # 0xff 是非法 UTF-8 起始字节，喂入即报错。
        p = SSEParser()
        with self.assertRaises(UnicodeDecodeError):
            p.feed(b"data: \xff")

    def test_incomplete_sequence_at_eof_silently_dropped(self):
        # 合法前缀 + 截断的多字节序列（正是 fixture partial 场景）：
        # feed 时缓存不报错，end_stream 时把残留字节连同半条消息一起丢弃。
        p = SSEParser()
        self.assertEqual(p.feed(b"data: \xe6\xad"), [])
        self.assertEqual(p.end_stream(), [])

    def test_retry_non_integer_ignored(self):
        p = SSEParser()
        p.feed(b"retry: 100x\n\n")
        self.assertIsNone(p.retry_ms)
        p.feed(b"retry: 250\n\n")
        self.assertEqual(p.retry_ms, 250)

    def test_id_with_nul_ignored(self):
        p = SSEParser()
        evs = p.feed(b"id: bad\x00id\ndata: x\n\n")
        self.assertEqual(evs[0].event_id, None)

    def test_field_without_colon_treated_as_empty_value(self):
        p = SSEParser()
        evs = p.feed(b"event\ndata: x\n\n")
        self.assertEqual(evs[0].event, "")

    def test_optional_space_after_colon_only_one_removed(self):
        p = SSEParser()
        evs = p.feed(b"data:   two spaces kept one\n\n")
        self.assertEqual(evs[0].data, "  two spaces kept one")

    def test_heartbeat_mid_event_does_not_reset_builder(self):
        # 注释行不会结束事件块。
        raw = b"data: first\n: hb\ndata: second\n\n"
        (ev,) = parse_all([raw])
        self.assertEqual(ev.data, "first\nsecond")

    def test_fixture_like_stream_split_into_7_byte_chunks(self):
        # 复刻 fixture 的写入节奏：7 字节一片 + 心跳 + 多 data 行事件。
        payload = '{\n  "stream_id": "streamA",\n  "sequence": 1,\n  "value": "正常"\n}'
        lines = ["event: reading", "id: streamA:1"] + [
            "data: " + line for line in payload.splitlines()
        ]
        raw = ("\r\n".join(lines) + "\r\n\r\n").encode("utf-8")
        chunks = [raw[i:i + 7] for i in range(0, len(raw), 7)]
        events = parse_all(chunks)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].event_id, "streamA:1")
        self.assertIn("正常", events[0].data)


if __name__ == "__main__":
    unittest.main()
