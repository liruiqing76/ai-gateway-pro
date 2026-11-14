"""车道状态落盘: 重启后界面不该丢掉「车道畅通/关闭」徽标。"""
import json

import pytest

from backend.core import lane_state


@pytest.fixture(autouse=True)
def isolated_dir(tmp_path, monkeypatch):
    # 显式指定状态目录, 避免依赖数据库 URL 推导(见 test_state_paths)
    monkeypatch.setenv("GW_STATE_DIR", str(tmp_path))
    yield tmp_path


class TestLaneStateStore:
    def test_save_then_load(self):
        lane_state.save("opencode", "open", synced_at="2026-10-24 18:00:00")
        got = lane_state.get("opencode")
        assert got["state"] == "open"
        assert got["synced_at"] == "2026-10-24 18:00:00"

    def test_merge_does_not_clobber_other_channels(self):
        lane_state.save("opencode", "open")
        lane_state.save("kilo", "closed")
        data = lane_state.load()
        assert data["opencode"]["state"] == "open"
        assert data["kilo"]["state"] == "closed"

    def test_overwrite_same_channel(self):
        lane_state.save("kilo", "closed")
        lane_state.save("kilo", "open")
        assert lane_state.get("kilo")["state"] == "open"

    def test_unknown_channel_is_empty(self):
        assert lane_state.get("nope") == {}

    def test_corrupt_file_returns_empty(self, isolated_dir):
        (isolated_dir / "lane_state.json").write_text("{not json", encoding="utf-8")
        assert lane_state.load() == {}
        # 损坏后仍能正常写入(自愈)
        lane_state.save("opencode", "open")
        assert lane_state.get("opencode")["state"] == "open"

    def test_non_dict_json_returns_empty(self, isolated_dir):
        (isolated_dir / "lane_state.json").write_text("[1,2,3]", encoding="utf-8")
        assert lane_state.load() == {}

    def test_empty_args_ignored(self):
        lane_state.save("", "open")
        lane_state.save("opencode", "")
        assert lane_state.load() == {}

    def test_extra_fields_persisted_but_none_dropped(self):
        lane_state.save("kilo", "open", usable=14, synced_at=None)
        got = lane_state.get("kilo")
        assert got["usable"] == 14
        assert "synced_at" not in got

    def test_file_is_readable_json(self, isolated_dir):
        lane_state.save("opencode", "closed")
        raw = json.loads((isolated_dir / "lane_state.json").read_text(encoding="utf-8"))
        assert raw["opencode"]["state"] == "closed"
