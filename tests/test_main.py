""" Tests of command line program

:Author: Jonathan Karr <karr@mssm.edu>
:Date: 2019-09-11
:Copyright: 2019, Karr Lab
:License: MIT
"""

from obj_model import __main__
from obj_model import io
from obj_model import utils
import capturer
import importlib
import mock
import obj_model
import os.path
import shutil
import tempfile
import unittest
import wc_utils.workbook.io


class TestCli(unittest.TestCase):

    def setUp(self):
        self.tempdir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tempdir)

    def test_raw_cli(self):
        with mock.patch('sys.argv', ['obj-model', '--help']):
            with capturer.CaptureOutput(relay=False):
                with self.assertRaises(SystemExit) as context:
                    __main__.main()
                    self.assertRegex(context.Exception, 'usage: obj-model')

        with mock.patch('sys.argv', ['obj-model']):
            with capturer.CaptureOutput(relay=False) as capture_output:
                __main__.main()
                self.assertRegex(capture_output.get_text(), 'usage: obj-model')

    def test_get_version(self):
        with capturer.CaptureOutput(relay=False, termination_delay=0.1) as capture_output:
            with __main__.App(argv=['-v']) as app:
                with self.assertRaises(SystemExit):
                    app.run()
                self.assertEqual(capture_output.get_text(), obj_model.__version__)

        with capturer.CaptureOutput(relay=False, termination_delay=0.1) as capture_output:
            with __main__.App(argv=['--version']) as app:
                with self.assertRaises(SystemExit):
                    app.run()
                self.assertEqual(capture_output.get_text(), obj_model.__version__)

    def test_convert(self):
        csv_file = os.path.join('tests', 'fixtures', 'schema.csv')
        py_file = os.path.join(self.tempdir, 'schema.py')
        with __main__.App(argv=['init-schema', csv_file, py_file, '--sbtab']) as app:
            app.run()
        schema = utils.get_schema(py_file)
        models = list(utils.get_models(schema).values())

        xl_file_1 = os.path.join(self.tempdir, 'file1.xlsx')
        p_0 = schema.Parent(id='p_0')
        p_0.children.create(id='c_0')
        p_0.children.create(id='c_1')
        io.WorkbookWriter().run(xl_file_1, [p_0], models=models, sbtab=True)
        csv_file_2 = os.path.join(self.tempdir, 'file2-*.csv')
        with __main__.App(argv=['convert', csv_file, xl_file_1, csv_file_2, '--sbtab']) as app:
            app.run()

        p_0_b = io.WorkbookReader().run(csv_file_2,
                                        models=models,
                                        sbtab=True,
                                        **io.SBTAB_DEFAULT_READER_OPTS)[schema.Parent][0]
        self.assertTrue(p_0_b.is_equal(p_0))

    def test_diff(self):
        csv_file = os.path.join('tests', 'fixtures', 'schema.csv')
        py_file = os.path.join(self.tempdir, 'schema.py')
        with __main__.App(argv=['init-schema', csv_file, py_file, '--sbtab']) as app:
            app.run()
        schema = utils.get_schema(py_file)
        models = list(utils.get_models(schema).values())

        xl_file_1 = os.path.join(self.tempdir, 'file1.xlsx')
        p_0 = schema.Parent(id='p_0')
        p_0.children.create(id='c_0', name='c_0')
        p_0.children.create(id='c_1', name='c_1')
        io.WorkbookWriter().run(xl_file_1, [p_0], models=models, sbtab=True)

        xl_file_2 = os.path.join(self.tempdir, 'file2.xlsx')
        p_0 = schema.Parent(id='p_0')
        p_0.children.create(id='c_0', name='c_0')
        p_0.children.create(id='c_1', name='c_0')
        io.WorkbookWriter().run(xl_file_2, [p_0], models=models, sbtab=True)

        xl_file_3 = os.path.join(self.tempdir, 'file3.xlsx')
        p_0 = schema.Parent(id='p_0')
        p_0.children.create(id='c_0', name='c_0')
        p_0.children.create(id='c_1', name='c_1')
        p_0.children.create(id='c_2', name='c_2')
        io.WorkbookWriter().run(xl_file_3, [p_0], models=models, sbtab=True)

        with __main__.App(argv=['diff', csv_file, 'Parent', xl_file_1, xl_file_1, '--sbtab']) as app:
            app.run()

        with self.assertRaises(SystemExit):
            with __main__.App(argv=['diff', csv_file, 'Parent2', xl_file_1, xl_file_1, '--sbtab']) as app:
                app.run()

        with self.assertRaises(SystemExit):
            with __main__.App(argv=['diff', csv_file, 'Parent', xl_file_1, xl_file_2, '--sbtab']) as app:
                app.run()

        with self.assertRaises(SystemExit):
            with __main__.App(argv=['diff', csv_file, 'Child', xl_file_1, xl_file_3, '--sbtab']) as app:
                app.run()

        with self.assertRaises(SystemExit):
            with __main__.App(argv=['diff', csv_file, 'Child', xl_file_3, xl_file_1, '--sbtab']) as app:
                app.run()

    def test_init_schema(self):
        csv_file = os.path.join('tests', 'fixtures', 'schema.csv')
        py_file = os.path.join(self.tempdir, 'schema.py')
        with __main__.App(argv=['init-schema', csv_file, py_file, '--sbtab']) as app:
            app.run()

        schema = utils.get_schema(py_file)
        self.assertEqual(sorted(utils.get_models(schema)), ['Child', 'Parent', 'Quantity'])

    def test_gen_template(self):
        csv_file = os.path.join('tests', 'fixtures', 'schema.csv')
        xl_file = os.path.join(self.tempdir, 'file.xlsx')
        with __main__.App(argv=['gen-template', csv_file, xl_file, '--sbtab']) as app:
            app.run()

        py_file = os.path.join(self.tempdir, 'schema.py')
        with __main__.App(argv=['init-schema', csv_file, py_file, '--sbtab']) as app:
            app.run()
        schema = utils.get_schema(py_file)

        objs = io.WorkbookReader().run(xl_file,
                                       models=list(utils.get_models(schema).values()),
                                       sbtab=True,
                                       **io.SBTAB_DEFAULT_READER_OPTS)
        self.assertEqual(objs, {
            schema.Parent: [],
            schema.Child: [],
            schema.Quantity: [],
        })

        csv_file = os.path.join(self.tempdir, 'file-*.xlsx')
        with __main__.App(argv=['gen-template', py_file, csv_file, '--sbtab']) as app:
            app.run()
        objs = io.WorkbookReader().run(csv_file, models=list(utils.get_models(schema).values()),
                                       group_objects_by_model=False,
                                       sbtab=True,
                                       **io.SBTAB_DEFAULT_READER_OPTS)
        self.assertEqual(objs, None)

    def test_normalize(self):
        csv_file = os.path.join('tests', 'fixtures', 'schema.csv')
        py_file = os.path.join(self.tempdir, 'schema.py')
        with __main__.App(argv=['init-schema', csv_file, py_file, '--sbtab']) as app:
            app.run()
        schema = utils.get_schema(py_file)
        models = list(utils.get_models(schema).values())

        xl_file_1 = os.path.join(self.tempdir, 'file1.xlsx')
        p_0 = schema.Parent(id='p_0')
        p_0.children.create(id='c_0')
        p_0.children.create(id='c_1')
        io.WorkbookWriter().run(xl_file_1, [p_0], models=models, sbtab=True)

        xl_file_2 = os.path.join(self.tempdir, 'file2.xlsx')
        with __main__.App(argv=['normalize', csv_file, 'Parent', xl_file_1, xl_file_2, '--sbtab']) as app:
            app.run()

        p_0_b = io.WorkbookReader().run(xl_file_2,
                                        models=models,
                                        sbtab=True,
                                        **io.SBTAB_DEFAULT_READER_OPTS)[schema.Parent][0]
        self.assertTrue(p_0_b.is_equal(p_0))

        with self.assertRaises(SystemExit):
            with __main__.App(argv=['normalize', csv_file, 'Parent2', xl_file_1, xl_file_2, '--sbtab']) as app:
                app.run()

    def test_validate(self):
        csv_file = os.path.join('tests', 'fixtures', 'schema.csv')
        py_file = os.path.join(self.tempdir, 'schema.py')
        with __main__.App(argv=['init-schema', csv_file, py_file, '--sbtab']) as app:
            app.run()
        schema = utils.get_schema(py_file)
        models = list(utils.get_models(schema).values())

        xl_file_1 = os.path.join(self.tempdir, 'file1.xlsx')
        p_0 = schema.Parent(id='p_0')
        p_0.children.create(id='c_0')
        p_0.children.create(id='c_1')
        io.WorkbookWriter().run(xl_file_1, [p_0], models=models, sbtab=True)
        with __main__.App(argv=['validate', csv_file, xl_file_1, '--sbtab']) as app:
            app.run()

        xl_file_2 = os.path.join(self.tempdir, 'file2.xlsx')
        wb = wc_utils.workbook.io.read(xl_file_1)
        wb['!Child'][4][0] = 'c_0'
        wc_utils.workbook.io.write(xl_file_2, wb)
        with self.assertRaises(SystemExit):
            with __main__.App(argv=['validate', csv_file, xl_file_2, '--sbtab']) as app:
                app.run()
