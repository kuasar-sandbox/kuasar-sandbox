#!/usr/bin/env python3
import hashlib,importlib.util,io,json,os,pathlib,subprocess,tarfile,tempfile,unittest
spec=importlib.util.spec_from_file_location('executor_transport',pathlib.Path(__file__).with_name('executor_transport.py'))
transport=importlib.util.module_from_spec(spec);spec.loader.exec_module(transport)
class ExecutorTransportTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=pathlib.Path(self.tmp.name);self.source=self.root/'source';self.source.mkdir()
        self.git('init','--quiet','--template=')
        for key,value in [('user.name','Executor fixture'),('user.email','fixture@example.invalid'),('commit.gpgsign','false'),('core.hooksPath','/dev/null')]:
            self.git('config',key,value)
        self.write('ci/integration/executable.sh','#!/bin/sh\nexit 0\n',0o755)
        self.write('ci/integration/ignored.py','retained despite export-ignore\n')
        self.write('ci/integration/subst.txt','$Format:%H$\n')
        self.write('outside/not-materialized.txt','object only\n')
        self.write('.gitattributes','ci/integration/ignored.py export-ignore\nci/integration/subst.txt export-subst\n')
        self.commit()
    def git(self,*args,input=None):
        p=subprocess.run(['git','-C',str(self.source),*args],env=transport.ENV,input=input,
                         capture_output=True,text=True,check=True)
        return p.stdout.strip()
    def write(self,name,data,mode=0o644):
        p=self.source/name;p.parent.mkdir(parents=True,exist_ok=True);p.write_text(data);p.chmod(mode)
    def commit(self):
        self.git('add','-A');self.git('commit','--quiet','-m','fixture')
        self.sha=self.git('rev-parse','HEAD')
    def package(self,name='executor.tar.gz'):
        path=self.root/name
        result=transport.package(self.source,self.sha,path)
        return path,result
    def restore(self,path,result,destination=None):
        return transport.restore(path,result['framework_sha'],result['framework_tree'],result['archive_sha256'],
                                 destination or self.root/'framework')
    def rewritten(self,path,change):
        destination=self.root/'changed.tar.gz'
        with tarfile.open(path,'r:gz')as source,tarfile.open(destination,'w:gz')as out:
            for member in source:
                data=source.extractfile(member).read()if member.isfile()else None
                member,data=change(member,data)
                if data is not None:member.size=len(data)
                out.addfile(member,io.BytesIO(data)if data is not None else None)
        return destination
    def test_real_objects_modes_export_attributes_and_sparse_scope(self):
        path,result=self.package();self.restore(path,result)
        target=self.root/'framework'
        self.assertEqual((target/'ci/integration/ignored.py').read_text(),'retained despite export-ignore\n')
        self.assertEqual((target/'ci/integration/subst.txt').read_text(),'$Format:%H$\n')
        self.assertEqual((target/'ci/integration/executable.sh').stat().st_mode&0o777,0o755)
        self.assertFalse((target/'outside').exists())
        self.assertEqual(transport.git(target,'rev-parse','HEAD').decode().strip(),self.sha)
        self.assertEqual(transport.git(target,'cat-file','commit','HEAD'),transport.git(self.source,'cat-file','commit','HEAD'))
        self.assertFalse(transport.git(target,'status','--porcelain').strip())
        transport.git(target,'fsck','--full','--no-reflogs')
    def test_no_source_remote_or_credentials_are_copied(self):
        self.git('remote','add','origin','https://github.com/kuasar-sandbox/kuasar-sandbox.git')
        self.git('config','http.extraHeader','fixture-only-value')
        path,result=self.package();self.restore(path,result)
        config=(self.root/'framework/.git/config').read_text()
        self.assertNotIn('fixture-only-value',config);self.assertNotIn('github.com',config)
        self.assertFalse(transport.git(self.root/'framework','remote').strip())
    def test_dirty_source_is_rejected(self):
        self.write('ci/integration/ignored.py','dirty\n')
        with self.assertRaisesRegex(ValueError,'dirty'):self.package()
    def test_wrong_source_sha_is_rejected(self):
        with self.assertRaisesRegex(ValueError,'identity'):transport.package(self.source,'0'*40,self.root/'bad.tar.gz')
    def test_symlink_is_rejected(self):
        os.symlink('ignored.py',self.source/'ci/integration/link');self.commit()
        with self.assertRaisesRegex(ValueError,'symlinks'):self.package()
    def test_gitlink_is_rejected(self):
        nested=self.source/'nested';nested.mkdir()
        transport.git(nested,'init','--quiet','--template=')
        transport.git(nested,'fetch','--quiet',str(self.source),self.sha)
        transport.git(nested,'checkout','--quiet','--detach',self.sha)
        self.git('update-index','--add','--cacheinfo','160000,'+self.sha+',nested')
        self.git('commit','--quiet','-m','gitlink');self.sha=self.git('rev-parse','HEAD')
        with self.assertRaisesRegex(ValueError,'gitlinks'):self.package()
    def test_existing_output_is_unchanged(self):
        p=self.root/'executor.tar.gz';p.write_text('sentinel')
        with self.assertRaisesRegex(ValueError,'already exists'):self.package()
        self.assertEqual(p.read_text(),'sentinel')
    def test_truncated_archive_fails_digest_before_promotion(self):
        p,r=self.package();p.write_bytes(p.read_bytes()[:-8])
        with self.assertRaisesRegex(ValueError,'digest'):self.restore(p,r)
        self.assertFalse((self.root/'framework').exists())
    def test_wrong_commit_and_tree_fail_before_promotion(self):
        p,r=self.package()
        for field in ['framework_sha','framework_tree']:
            changed=dict(r);changed[field]='0'*40
            with self.assertRaises(ValueError):self.restore(p,changed)
        self.assertFalse((self.root/'framework').exists())
    def test_worktree_tampering_is_detected(self):
        p,r=self.package()
        changed=self.rewritten(p,lambda m,d:(m,b'altered\n')if m.name=='framework/ci/integration/ignored.py'else(m,d))
        r['archive_sha256']=hashlib.sha256(changed.read_bytes()).hexdigest()
        with self.assertRaises(ValueError):self.restore(changed,r)
        self.assertFalse((self.root/'framework').exists())
    def test_unsafe_members_fail_without_escape(self):
        for name,kind,mode in [('../escape','file',0o644),('framework/link','symlink',0o644),('framework/setuid','file',0o4755)]:
            p=self.root/'unsafe.tar.gz'
            with tarfile.open(p,'w:gz')as tar:
                m=tarfile.TarInfo(name);m.mode=mode
                if kind=='symlink':m.type=tarfile.SYMTYPE;m.linkname='/tmp'
                else:m.size=1
                tar.addfile(m,None if kind=='symlink'else io.BytesIO(b'x'))
            with self.assertRaises(ValueError):
                transport.restore(p,self.sha,'0'*40,hashlib.sha256(p.read_bytes()).hexdigest(),self.root/'framework')
        self.assertFalse((self.root/'escape').exists())
    def test_unrelated_existing_directory_is_preserved(self):
        p,r=self.package();target=self.root/'framework';target.mkdir();(target/'sentinel').write_text('keep')
        with self.assertRaisesRegex(ValueError,'not a job Git checkout'):self.restore(p,r)
        self.assertEqual((target/'sentinel').read_text(),'keep')
    def test_symlink_destination_is_preserved(self):
        p,r=self.package();outside=self.root/'outside';outside.mkdir();os.symlink(outside,self.root/'framework')
        with self.assertRaisesRegex(ValueError,'symlink'):self.restore(p,r)
        self.assertTrue((self.root/'framework').is_symlink());self.assertEqual(list(outside.iterdir()),[])
if __name__=='__main__':unittest.main()
