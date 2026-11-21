"""项目关系与管理接口，使用临时 SQLite。"""
from dataclasses import replace

from src import admin_audit, auth_db, business_directions, projects
from tests._helpers import TempAuthDB
from tests.test_admin_api import ApiTestCase


class ProjectDataTests(TempAuthDB):
    def setUp(self):
        super().setUp()
        self.direction = business_directions.create_direction('低空经济')['id']
        self.lead = self.make_department('研发部')['id']
        self.participant = self.make_department('交付部')['id']
        self.fields = projects.ProjectFields('低空监管平台', self.direction, self.lead,
                                             [self.lead, self.participant, self.participant], '')

    def test_reuse_department_and_direction_without_duplicating_project(self):
        first = projects.save_project(self.fields)
        second = projects.save_project(replace(self.fields, name='低空预警平台'))
        self.assertEqual([p['id'] for p in projects.list_projects()], [second['id'], first['id']])
        self.assertEqual(first['direction_name'], '低空经济')
        self.assertEqual(first['lead_department_name'], '研发部')
        self.assertEqual(first['participants'], [{'id': self.participant, 'name': '交付部'}])
        with self.assertRaisesRegex(ValueError, '项目名称已存在'):
            projects.save_project(self.fields)

    def test_invalid_or_stale_edit_preserves_existing_relations(self):
        project = projects.save_project(self.fields)
        args = {'project_id': project['id'], 'revision': project['revision']}
        with self.assertRaisesRegex(ValueError, '部门不存在'):
            projects.save_project(replace(self.fields, participant_department_ids=[999]), **args)
        self.assertEqual(projects.get_project(project['id']), project)
        edited = projects.save_project(replace(self.fields, participant_department_ids=[]), **args)
        self.assertEqual(edited['participants'], [])
        with self.assertRaises(projects.ProjectConflict):
            projects.save_project(self.fields, **args)
        self.assertEqual(projects.get_project(project['id']), edited)

    def test_existing_disabled_department_can_be_retained_but_not_newly_assigned(self):
        project = projects.save_project(self.fields)
        auth_db.set_department_disabled(self.participant, True)
        with self.assertRaisesRegex(ValueError, '不能新增已停用'):
            projects.save_project(replace(self.fields, name='新项目'))
        with self.assertRaisesRegex(ValueError, '仍关联项目'):
            auth_db.delete_department(self.participant)
        current = projects.save_project(replace(self.fields, note='更新备注'),
                                        project_id=project['id'], revision=project['revision'])
        self.assertEqual(len(current['participants']), 1)
        projects.save_project(replace(self.fields, participant_department_ids=[]),
                              project_id=project['id'], revision=current['revision'])
        auth_db.delete_department(self.participant)
        self.assertIsNone(auth_db.get_department(self.participant))


class ProjectApiTests(ApiTestCase):
    def test_admin_create_detail_list_and_edit(self):
        self.assertEqual(self.client.get('/api/projects').status_code, 401)
        self.assertEqual(self.client.post('/api/projects', json={'name': '未登录项目'}).status_code, 401)
        self.login_admin()
        direction = business_directions.create_direction('低空经济')
        department = self.make_department('交付部')
        body = {'name': '低空监管平台', 'direction_id': direction['id'],
                'participant_department_ids': [department['id']]}
        saved = self.client.post('/api/projects', json=body)
        self.assertEqual(saved.status_code, 200, saved.text)
        project = saved.json()['project']
        url = f"/api/projects/{project['id']}"
        self.assertEqual(self.client.get(url).json()['project'], project)
        self.assertEqual(self.client.get('/api/projects').json()['projects'], [project])

        edit = {**body, 'note': '一期', 'revision': project['revision']}
        updated = self.client.put(url, json=edit)
        self.assertEqual(updated.status_code, 200, updated.text)
        self.assertEqual(updated.json()['project']['note'], '一期')
        self.assertEqual(self.client.put(url, json=edit).status_code, 409)
        self.assertEqual(admin_audit.list_operations(action='project_update', success=True)['total'], 1)
