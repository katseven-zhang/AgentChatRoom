import pytest

from agentchatroom.errors import DomainError
from agentchatroom.services import AgentChatRoomService


def test_legacy_whitespace_is_compared_symmetrically():
    criteria = [{'criterion': '验收完成', 'status': 'passed'}]
    assessed = AgentChatRoomService._review_criteria_map(criteria)
    diagnostics = AgentChatRoomService._review_criteria_diagnostics(['\u3000验收完成\n'], criteria, assessed)
    assert not diagnostics['unassessed_criteria']
    assert not diagnostics['unmatched_criteria']


@pytest.mark.parametrize('expected,submitted', [('é', 'e\u0301'), ('a b', 'a  b'), ('A', 'a')])
def test_no_implicit_unicode_case_or_internal_whitespace_matching(expected, submitted):
    criteria = [{'criterion': submitted, 'status': 'passed'}]
    diagnostics = AgentChatRoomService._review_criteria_diagnostics(
        [expected], criteria, AgentChatRoomService._review_criteria_map(criteria))
    assert diagnostics['unmatched_criteria'] == [submitted]
    assert diagnostics['unassessed_criteria'] == [expected]


@pytest.mark.parametrize('submitted,code,field', [
    ([('验收（中文）', 'passed'), ('第二条', 'passed')], None, None),
    ([('验收(中文)', 'passed'), ('第二条', 'passed')], 'acceptance_criteria_not_satisfied', 'unmatched_criteria'),
    ([('验收（中文）', 'passed')], 'acceptance_criteria_not_satisfied', 'unassessed_criteria'),
    ([('验收（中文）', 'failed'), ('第二条', 'passed')], 'acceptance_criteria_not_satisfied', 'not_passed_criteria'),
    ([('验收（中文）', 'failed'), ('验收（中文） ', 'passed')], 'duplicate_review_criterion', 'duplicate_criteria'),
])
def test_review_matching_and_diagnostics(service, project, joined_agents, submitted, code, field):
    executor, reviewer = joined_agents
    auth = dict(session_id=executor['agent']['id'], token=executor['token'])
    task = service.create_task(project['id'], title='Criteria regression',
        acceptance_criteria=['  验收（中文）  ', '第二条'],
        actor_session_id=auth['session_id'], token=auth['token'])['task']
    service.claim_task(project['id'], task['id'], **auth)
    service.update_task(project['id'], task['id'], status='in_progress', **auth)
    service.submit_work_report(project['id'], task['id'], summary='Ready',
        files=['example.py'], tests=[{'command': 'pytest', 'exit_code': 0}], **auth)
    before = service.list_events(project['id'])['events']
    def review():
        return service.submit_review(project['id'], task['id'],
            reviewer_session_id=reviewer['agent']['id'], token=reviewer['token'],
            verdict='approved', criteria=[{'criterion': c, 'status': s} for c, s in reversed(submitted)])
    if code is None:
        assert review()['task_status'] == 'verified'
    else:
        with pytest.raises(DomainError) as error:
            review()
        assert error.value.code == code
        assert error.value.details[field]
        if field == 'unmatched_criteria':
            assert error.value.details['differences'][0]['submitted_codepoint'] == 'U+0028'
            assert error.value.details['differences'][0]['expected_codepoint'] == 'U+FF08'
        assert service.list_events(project['id'])['events'] == before
