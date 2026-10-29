from school_mcp.server import server


async def test_mcp_lists_tools():
    names = {t.name for t in await server.list_tools()}
    assert {"get_due", "download_file", "submit_assignment", "send_file_to_me", "find_course",
            "group_chat_search", "teams_recent", "action_status", "get_deadlines", "search_material",
            "get_material", "list_material"} <= names
    assert not any("approve" in n or "execute" in n for n in names)
