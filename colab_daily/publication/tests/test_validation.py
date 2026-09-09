"""Synthetic real-coordinator -> isolated-refine interface -> durable final renderer."""
import copy
import json
from pathlib import Path
import tempfile
import unittest
from PIL import Image, PngImagePlugin

from colab_daily.config import Config
from colab_daily.lifecycle import Lifecycle, main as lifecycle_main
from colab_daily.storage import StorageError
from colab_daily.storage.tests import test_metadata_boundary as runtime_fixture
from colab_daily.publication.validation import (freeze, complete_refine, SECTIONS, IMAGE_STAGES, markdown,
                                               public_url, image_info, grouped, asset_key)
from colab_daily.publication.render import render

SENTINEL = 'SYNTHETIC_SOURCE_BODY_NOT_FINAL_9251'
PROJECT = Path(__file__).resolve().parents[3]


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False))


class PublicationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.config = Config.load(self.root, {})
        self.life = Lifecycle(self.config)
        self.life.begin('manual', 'owner', '2026-01-02')
        # This invokes the real grouped coordinator with its existing synthetic rating fixture,
        # including interrupted prepare recovery, not fabricated all-true validator flags.
        runtime_fixture.MetadataBoundaryTests.test_actual_three_track_coordinator_accepts_metadata_adapter(self)
        self.cycle = 'daily-2026-01-02'
        self.refine = self.life.working / 'refine_candidates/runs/refine-run'
        self.assembly = self.refine / 'assemblies/assembly-1/publication_set.json'
        self.contexts = {}
        self.manifest = {'interface_version': 1, 'cycle_id': self.cycle, 'refine_run_id': 'refine-run',
                         'assembly_generation_id': 'assembly-1', 'rating_run_id': 'rating-run', 'contexts': {},
                         'taxonomy_path': 'refine_candidates/runs/refine-run/assemblies/assembly-1/keyword_taxonomy.json',
                         'under_target_reason': '合成小集合只需要两个规范技术标签。'}
        self.publication = {'schema_version': 3, 'quota_contract': 'three-track-v3', 'cycle_id': self.cycle,
                            'display_date': '2026-01-02', 'selection_limit': 20, 'groups': {}}
        self.taxonomy = {'interface_version': 1, 'terms': ['机器人', '人工智能'], 'assignments': {},
                         'under_target_reason': self.manifest['under_target_reason']}
        selection = json.loads((self.life.working / 'rating_filter_organize/runs/rating-run/grouped_selection.json').read_text())
        for category in SECTIONS:
            track = category.lower()
            root = self.refine / f'contexts/{track}/generations/generation-1'
            root.mkdir(parents=True)
            source = root / 'source.md'
            source.write_text(SENTINEL + ' synthetic retained official/full text acquisition response')
            article = root / 'article.md'
            article.write_text('\n\n'.join('## ' + section + '\n\n完整的合成最终说明。 [来源](https://example.org/source)' for section in SECTIONS[category]))
            rel = lambda path: path.relative_to(self.life.working).as_posix()
            audit = {'stages': [{'stage': name, 'status': 'unavailable', 'url': 'https://example.org/' + name,
                                'path': rel(source), 'reason': '合成获取记录表明当前路径没有合适的图片。'} for name in IMAGE_STAGES[category]],
                     'nullReason': '所有既定获取路径均已检查，合成材料没有合适的图。', 'selected': None}
            item = {'candidate_id': track + '-1', 'title': '合成“完整”文章: ' + category, 'authors': ['Synthetic Author'],
                    'summary': '合成摘要，陈述事实与明确的证据限制。', 'sources': [{'name': 'Synthetic official source', 'url': 'https://example.org/source'}],
                    'content_path': rel(article), 'evidence': [{'kind': 'full_text' if category == 'Paper' else 'official',
                                                             'url': 'https://example.org/source', 'path': rel(source)}], 'image_audit': audit}
            context_id = track + '-refine-context'
            run_id = 'refine-' + track
            self.life.store.claim_run(self.cycle, run_id, 'refine:' + track, context_id, {'context_id': context_id})
            data = {'interface_version': 1, 'cycle_id': self.cycle, 'refine_run_id': 'refine-run', 'track': track,
                    'generation_id': 'generation-1', 'context_id': context_id, 'successful': [item], 'dropped': []}
            manifest_path = root / 'generation_manifest.json'
            self.contexts[track] = (manifest_path, data)
            self.manifest['contexts'][track] = {'run_id': run_id, 'context_id': context_id, 'generation_id': 'generation-1', 'manifest_path': rel(manifest_path)}
            original = selection['groups'][category]['candidates'][0]
            row = {k: original[k] for k in ('candidate_id', 'category', 'group_rank', 'group_score', 'score_scale', 'rating_track')}
            row.update({k: item[k] for k in ('title', 'authors', 'summary', 'sources', 'content_path')})
            row.update(keywords=['机器人', '人工智能'], preview_image=None)
            self.publication['groups'][category] = {'candidates': [row]}
            self.taxonomy['assignments'][row['candidate_id']] = row['keywords']

    def tearDown(self):
        self.temp.cleanup()

    def seal(self):
        for track, (path, data) in self.contexts.items():
            write(path, data)
            complete_refine(self.config, 'owner', 'refine-' + track, path)
        write(self.assembly, self.publication)
        write(self.assembly.parent / 'manifest.json', self.manifest)
        write(self.assembly.parent / 'keyword_taxonomy.json', self.taxonomy)

    def freeze(self):
        self.seal()
        return freeze(self.config, 'owner', self.assembly)

    def test_real_contract_freeze_retry_and_durable_render_after_temp_loss(self):
        digest = self.freeze()
        self.assertEqual(freeze(self.config, 'owner', self.assembly), digest)
        rendered = render(self.life.store, self.cycle)
        manifest = json.loads(rendered['docs/daily/2026-01-02/.managed-manifest.json'])
        self.assertEqual(list(manifest)[:6], ['schema_version', 'quota_contract', 'quota_revision', 'cycle_id', 'display_date', 'selection_limit'])
        self.assertEqual(list(manifest['groups']), ['Paper', 'News', 'Policy'])
        self.assertEqual(manifest['quota_proof']['policy_capacity'], 3)
        self.assertEqual(manifest['public_fields'], 'rank-display-v1')
        rendered_text = b'\n'.join(rendered.values())
        for forbidden in (b'groupScore', b'group_score', b'scoreScale', b'score_scale', b'ratingTrack', b'rating_track', b'frozen_sha256'):
            self.assertNotIn(forbidden, rendered_text)
        frozen = self.life.store.export_publication(self.cycle)
        paper = next(row for row in frozen['publication']['records'] if row['category'] == 'Paper')
        self.assertEqual(paper['canonical_url'], 'https://example.com/paper-1')
        self.assertEqual(paper['normalized_arxiv_id'], '2601.00001')
        self.assertEqual(paper['source_identities'], ['https://example.com/paper-1'])
        self.assertEqual(self.life.store.status(self.cycle)['phase'], 'PHASE_release')
        with self.assertRaises(StorageError): self.life.store.release(self.cycle)
        with self.life.store.connection() as conn:
            dump = '\n'.join(conn.iterdump())
        self.assertNotIn(SENTINEL, dump)
        self.assertNotIn('image_audit', dump)
        self.assertNotIn('successful', dump)
        self.assertIn('完整的合成最终说明', dump)
        import shutil
        # Synthetic loss simulation, not authorized production cleanup.
        shutil.rmtree(self.life.working)
        self.assertEqual(render(self.life.store, self.cycle), rendered)
        with self.assertRaises((StorageError, OSError)): freeze(self.config, 'owner', self.assembly)
        with self.assertRaises(StorageError): self.life.cleanup('owner', self.cycle)

    def test_safe_yaml_title_and_body(self):
        row = self.publication['groups']['Paper']['candidates'][0]
        row['title'] = 'Quoted "title": [safe] # not YAML\nsecond line'
        self.contexts['paper'][1]['successful'][0]['title'] = row['title']
        self.freeze()
        files = render(self.life.store, self.cycle)
        page = next(v.decode() for k, v in files.items() if '/paper/' in k)
        self.assertIn('title: "Quoted \\"title\\": [safe] # not YAML\\nsecond line"', page)

    def test_successful_actual_image_is_durable(self):
        path, data = self.contexts['paper']
        image = path.parent / 'preview.png'
        metadata = PngImagePlugin.PngInfo()
        metadata.add_text('Comment', 'PRIVATE_IMAGE_METADATA_SENTINEL')
        Image.new('RGB', (64, 48), 'blue').save(image, pnginfo=metadata)
        relative = image.relative_to(self.life.working).as_posix()
        audit = data['successful'][0]['image_audit']
        audit['stages'][0].update(status='found', path=relative)
        audit['nullReason'] = None
        audit['selected'] = {'path': relative, 'url': 'https://example.org/figure.png', 'kind': 'figure', 'caption': '实际合成测试图示'}
        self.publication['groups']['Paper']['candidates'][0]['preview_image'] = relative
        self.freeze()
        rendered = render(self.life.store, self.cycle)
        image_path = 'docs/public/daily/2026-01-02/assets/paper-1/preview.png'
        self.assertNotEqual(rendered[image_path], image.read_bytes())
        self.assertNotIn(b'PRIVATE_IMAGE_METADATA_SENTINEL', rendered[image_path])
        self.assertEqual(image_info(image)['width'], 64)
        frozen = self.life.store.export_publication(self.cycle)
        blob = self.life.store.root / frozen['assets']['images/paper-1/preview.png']['path']
        self.assertNotIn(b'PRIVATE_IMAGE_METADATA_SENTINEL', blob.read_bytes())

    def test_drop_requires_multipath_and_preserves_remaining_order(self):
        data = self.contexts['policy'][1]
        item = data['successful'].pop()
        data['dropped'] = [{'candidate_id': item['candidate_id'], 'reason_code': 'identity_unconfirmed', 'reason': '多路径仍无法确认合成对象的基本身份。',
                            'evidence': item['evidence'] + [{**item['evidence'][0], 'url': 'https://example.org/alternate'}]}]
        self.publication['groups']['Policy']['candidates'] = []
        del self.taxonomy['assignments']['policy-1']
        self.freeze()
        frozen = self.life.store.export_publication(self.cycle)
        self.assertEqual(frozen['validation']['counts']['dropped'], 1)
        self.assertEqual(len(frozen['publication']['records']), 2)

    def test_lifecycle_cli_claims_distinct_refine_context_owner(self):
        context = self.life.working / 'input/extra-refine-context.json'
        write(context, {'context_id': 'extra-paper-context'})
        result = lifecycle_main([
            '--project-root', str(self.root), 'claim-run', '--owner', 'owner', '--run', 'refine-extra-paper',
            '--kind', 'refine:extra', '--run-owner', 'extra-paper-context', '--context',
            context.relative_to(self.root).as_posix(),
        ])
        self.assertEqual(result, 0)
        run = self.life.store.run_state('refine-extra-paper')
        self.assertEqual(run['owner'], 'extra-paper-context')
        self.assertEqual(run['context']['context_id'], 'extra-paper-context')

    def test_active_policy_contracts_consistently_use_capacity_three(self):
        consensus = (PROJECT / 'consensus.md').read_text()
        policy = (PROJECT / 'policy_consensus.md').read_text()
        phase = (PROJECT / '.agents/skills/phase-release/SKILL.md').read_text()
        self.assertIn('Policy target/最大值为 3', consensus)
        self.assertNotIn('Policy target/最大值为 5', consensus)
        self.assertRegex(policy, r'Policy[^\n]{0,120}(?:3|三)')
        self.assertIn('Policy≤3', phase)

    def test_rescoring_fails(self):
        self.publication['groups']['Paper']['candidates'][0]['group_score'] -= 1
        with self.assertRaisesRegex(StorageError, 'rescore'): self.freeze()

    def test_missing_terminal_context_fails(self):
        self.seal()
        with self.life.store.connection(write=True) as conn:
            conn.execute("DELETE FROM run_stages WHERE run_id='refine-news'")
        with self.assertRaisesRegex(StorageError, 'terminal'): freeze(self.config, 'owner', self.assembly)

    def test_fake_context_identity_fails(self):
        self.manifest['contexts']['news']['context_id'] = 'paper-refine-context'
        with self.assertRaisesRegex(StorageError, 'identity'): self.freeze()

    def test_missing_source_after_completion_fails_without_db_replay(self):
        self.seal()
        (self.contexts['paper'][0].parent / 'source.md').unlink()
        with self.assertRaises(StorageError): freeze(self.config, 'owner', self.assembly)

    def test_taxonomy_shortage_and_mapping_required(self):
        self.taxonomy['under_target_reason'] = ''
        with self.assertRaises(StorageError): self.freeze()

    def test_assembly_cannot_replace_final_body(self):
        self.publication['groups']['News']['candidates'][0]['content_path'] = '../escape.md'
        with self.assertRaises(StorageError): self.freeze()

    def test_image_audit_must_execute_all_stages(self):
        self.contexts['paper'][1]['successful'][0]['image_audit']['stages'].pop()
        with self.assertRaisesRegex(StorageError, 'stages'): self.freeze()

    def test_wrong_role_cannot_be_called_display_image(self):
        self.contexts['paper'][1]['successful'][0]['image_audit']['selected'] = {'path': 'bad', 'url': 'https://example.org/image', 'kind': 'generated', 'caption': 'bad'}
        with self.assertRaises(StorageError): self.freeze()

    def test_symlink_evidence_and_missing_full_text_rejected(self):
        data = self.contexts['paper'][1]
        data['successful'][0]['evidence'][0]['kind'] = 'attempt'
        with self.assertRaisesRegex(StorageError, 'full-text'): self.freeze()


class SafetyTests(unittest.TestCase):
    def test_public_urls(self):
        for value in ['http://127.0.0.1/x', 'http://2130706433/x', 'http://0x7f000001/x', 'https://user:pass@example.org/',
                      'https://example.org/?token=bad', 'https://localhost/x', 'https://[::1]/', 'file:///x', 'https://example.org\\@localhost/',
                      'https://example.org:444/', 'javascript:alert(1)']:
            with self.subTest(value=value), self.assertRaises(StorageError): public_url(value)
        self.assertEqual(public_url('https://example.org/source?q=robot'), 'https://example.org/source?q=robot')

    def test_markdown_reference_html_vue_workspace_and_sections(self):
        good = '\n\n'.join('## ' + s + '\n\n可读的合成文字。' for s in SECTIONS['News'])
        self.assertEqual(markdown(good, 'News'), good)
        for bad in [good + '\n[x][a]\n\n[a]: javascript:alert(1)', good + '\n<script>alert(1)</script>',
                    good + '\n{{ process.exit() }}', good + '\n[local](working_tmp/source.md)', good + '\n![image](https://example.org/x.png)',
                    good.replace('## 事件概述', '## 政策行动')]:
            with self.assertRaises(StorageError): markdown(bad, 'News')

    def test_image_bytes_extension_dimensions_and_symlinks(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            image = root / 'image.png'
            image.write_bytes(b'not an image')
            with self.assertRaises(StorageError): image_info(image)
            Image.new('RGB', (8, 8)).save(image)
            with self.assertRaises(StorageError): image_info(image)
            Image.new('RGB', (40, 40)).save(image)
            alias = root / 'alias.png'; alias.symlink_to(image)
            with self.assertRaises(StorageError): image_info(alias)
            jpeg = root / 'wrong.jpg'; jpeg.write_bytes(image.read_bytes())
            with self.assertRaises(StorageError): image_info(jpeg)

    def test_asset_identity_alias_is_not_decoded_path(self):
        self.assertEqual(asset_key('url--https%3A%2F%2Fexample.org'), 'url--https_3a_2f_2fexample.org')
        for value in ['../escape', '/absolute', 'x/y', 'x%xx']:
            with self.assertRaises(StorageError): asset_key(value)


if __name__ == '__main__': unittest.main()
