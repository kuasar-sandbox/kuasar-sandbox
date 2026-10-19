"""Package only the fixed sparse executor from existing exact Git objects; no network."""
import argparse,hashlib,json,os,pathlib,re,shutil,subprocess,tarfile,tempfile
SPARSE=('/.github/actions/download-artifact/','/ci/hosted/','/ci/integration/',
        '/release/tag_sources.py','/release/selection.py','/test/e2e/package_inputs.py',
        '/test/e2e/lib/','/workbench/')
ENV={'PATH':os.environ.get('PATH','/usr/bin:/bin'),'LC_ALL':'C','GIT_CONFIG_GLOBAL':'/dev/null',
     'GIT_CONFIG_NOSYSTEM':'1','GIT_ATTR_NOSYSTEM':'1','GIT_NO_LAZY_FETCH':'1'}
def require(ok,reason):
    if not ok:raise ValueError(reason)
def git(root,*args,input=None,stdout=subprocess.PIPE):
    p=subprocess.run(['git','-C',str(root),*args],env=ENV,input=input,stdout=stdout,
                     stderr=subprocess.PIPE,timeout=60)
    require(p.returncode==0,'Git object operation failed: '+p.stderr.decode(errors='replace')[:300])
    return p.stdout
def package(source,sha,output):
    source=pathlib.Path(source).resolve();output=pathlib.Path(output)
    require(re.fullmatch('[0-9a-f]{40}',sha),'invalid expected commit')
    require(git(source,'rev-parse','HEAD').decode().strip()==sha,'source checkout identity mismatch')
    require(not git(source,'status','--porcelain').strip(),'source executor checkout is dirty')
    tree=git(source,'rev-parse',sha+'^{tree}').decode().strip()
    require(not output.exists()and not output.is_symlink(),'archive output already exists')
    ids={sha,tree};count=0
    for row in git(source,'ls-tree','-rtz',sha).split(b'\0'):
        if not row:continue
        info,name=row.split(b'\t',1);mode,kind,object_id=info.split()
        require((mode,kind)in[(b'040000',b'tree'),(b'100644',b'blob'),(b'100755',b'blob')],
                'executor cannot contain symlinks, gitlinks or special modes')
        parts=name.decode().split('/')
        require(all(p and p not in ('.','..')and p.lower()!='.git'for p in parts),'unsafe tracked path')
        ids.add(object_id.decode());count+=1
    require(0<count<=10000,'object count bound')
    request=('\n'.join(sorted(ids))+'\n').encode()
    sizes=git(source,'cat-file','--batch-check=%(objectname) %(objecttype) %(objectsize)',input=request).splitlines()
    require(len(sizes)==len(ids),'missing Git objects')
    total=0
    for row in sizes:
        fields=row.split();require(len(fields)==3 and fields[1]in(b'blob',b'tree',b'commit'),'missing or unsupported Git object')
        size=int(fields[2]);require(size<=16*1024*1024,'object size bound');total+=size
    require(total<=64*1024*1024,'total object size bound')
    with tempfile.TemporaryDirectory(prefix='executor-package-',dir=output.parent.resolve())as directory:
        root=pathlib.Path(directory);pack=root/'objects.pack'
        with pack.open('wb')as target:git(source,'pack-objects','--stdout',input=request,stdout=target)
        require(pack.stat().st_size<=16*1024*1024,'pack size bound')
        checkout=root/'framework';checkout.mkdir()
        git(checkout,'init','--quiet','--template=')
        git(checkout,'config','core.hooksPath','/dev/null')
        git(checkout,'config','core.attributesFile','/dev/null')
        git(checkout,'config','core.logAllRefUpdates','false')
        git(checkout,'config','core.sparseCheckout','true')
        git(checkout,'config','core.sparseCheckoutCone','false')
        with pack.open('rb')as content:
            p=subprocess.run(['git','-C',str(checkout),'index-pack','--stdin'],env=ENV,
                             stdin=content,stdout=subprocess.PIPE,stderr=subprocess.PIPE,timeout=60)
            require(p.returncode==0,'Git pack import failed')
        (checkout/'.git/info').mkdir(exist_ok=True)
        (checkout/'.git/shallow').write_text(sha+'\n')
        (checkout/'.git/info/sparse-checkout').write_text('\n'.join(SPARSE)+'\n')
        git(checkout,'checkout','--quiet','--detach',sha)
        require(git(checkout,'rev-parse','HEAD').decode().strip()==sha and
                git(checkout,'rev-parse','HEAD^{tree}').decode().strip()==tree,'restored Git identity mismatch')
        git(checkout,'fsck','--full','--no-reflogs')
        require(not git(checkout,'status','--porcelain').strip(),'restored executor is not clean')
        selected=[p for p in checkout.rglob('*')if p.is_file()and'.git'not in p.relative_to(checkout).parts]
        require(selected and all(any('/'+str(p.relative_to(checkout))==s.rstrip('/')or
                    ('/'+str(p.relative_to(checkout))).startswith(s)for s in SPARSE)for p in selected),'sparse scope mismatch')
        def clean(info):
            require(info.isdir()or info.isfile(),'executor archive link or special member')
            info.uid=info.gid=0;info.uname=info.gname='';info.mtime=0;info.pax_headers={}
            info.mode=0o755 if info.isdir()or info.mode&0o111 else 0o644
            return info
        with tarfile.open(output,'w:gz')as tar:tar.add(checkout,arcname='framework',filter=clean)
    require(output.stat().st_size<=16*1024*1024,'executor archive size bound')
    return {'framework_sha':sha,'framework_tree':tree,'archive_sha256':hashlib.sha256(output.read_bytes()).hexdigest(),
            'archive_bytes':output.stat().st_size,'git_object_bytes':total,'git_object_count':len(ids),'sparse_files':len(selected),
            'scope':'Exact original Git objects packed by Git; no remote configuration or credentials'}

def restore(archive,sha,tree,digest,destination):
    """Verify in a private sibling before replacing this job's fixed framework path."""
    require(re.fullmatch('[0-9a-f]{40}',sha)and re.fullmatch('[0-9a-f]{40}',tree)
            and re.fullmatch('[0-9a-f]{64}',digest),'invalid executor identity')
    archive=pathlib.Path(archive);destination=pathlib.Path(destination).absolute()
    require(destination.name=='framework'and destination.parent.is_dir(),'destination must be the job framework directory')
    require(not destination.is_symlink(),'framework destination is a symlink')
    require(archive.is_file()and not archive.is_symlink()and 0<archive.stat().st_size<=16*1024*1024,'executor archive size/type bound')
    require(hashlib.sha256(archive.read_bytes()).hexdigest()==digest,'executor archive digest mismatch')
    parent=destination.parent.resolve()
    with tempfile.TemporaryDirectory(prefix='.executor-restore-',dir=parent)as directory:
        stage=pathlib.Path(directory);seen=set();total=0
        with tarfile.open(archive,'r:gz')as tar:
            for member in tar:
                parts=pathlib.PurePosixPath(member.name).parts
                require(parts and parts[0]=='framework'and not member.name.startswith('/')
                        and all(p not in ('.','..')for p in parts)
                        and len(parts)<=64 and len(member.name.encode())<=4096,'unsafe executor member path')
                require(member.name not in seen and len(seen)<10000,'duplicate or excessive executor members')
                seen.add(member.name)
                require(member.isdir()or member.isfile(),'executor archive link or special file')
                require(member.mode in (0o644,0o755),'unsafe executor mode')
                target=stage.joinpath(*parts)
                if member.isdir():
                    target.mkdir(parents=True,exist_ok=True);target.chmod(0o755);continue
                require(0<=member.size<=16*1024*1024,'executor member size bound')
                total+=member.size;require(total<=64*1024*1024,'expanded executor size bound')
                target.parent.mkdir(parents=True,exist_ok=True)
                data=tar.extractfile(member).read(16*1024*1024+1)
                require(len(data)==member.size,'truncated executor member')
                with target.open('xb')as output:output.write(data)
                target.chmod(member.mode)
        framework=stage/'framework'
        require((framework/'.git').is_dir()and not(framework/'.git').is_symlink(),'missing local Git object database')
        require(git(framework,'rev-parse','HEAD').decode().strip()==sha
                and git(framework,'rev-parse','HEAD^{tree}').decode().strip()==tree,'executor Git identity mismatch')
        git(framework,'fsck','--full','--no-reflogs')
        require(not git(framework,'remote').strip(),'executor must not contain remotes')
        require((framework/'.git/info/sparse-checkout').read_text()=='\n'.join(SPARSE)+'\n','executor sparse scope mismatch')
        git(framework,'update-index','--really-refresh')
        require(not git(framework,'status','--porcelain').strip(),'executor working tree differs from Git objects')
        require(destination.parent.resolve()==parent and not destination.is_symlink(),'framework destination changed')
        backup=stage/'previous-framework'
        if destination.exists():
            require(destination.is_dir()and(destination/'.git').is_dir()
                    and not(destination/'.git').is_symlink(),'existing destination is not a job Git checkout')
            require(git(destination,'rev-parse','--show-toplevel').decode().strip()==str(destination),
                    'existing framework uses another worktree')
            destination.rename(backup)
        try:framework.rename(destination)
        except BaseException:
            if backup.exists():backup.rename(destination)
            raise
        # TemporaryDirectory removes only the validated previous framework after promotion.
        return {'framework_sha':sha,'framework_tree':tree,'archive_sha256':digest,'destination':str(destination),
                'archive_bytes':archive.stat().st_size,'git_fsck':'passed','git_status':'clean'}

def main():
    p=argparse.ArgumentParser(description=__doc__)
    sub=p.add_subparsers(dest='command',required=True)
    a=sub.add_parser('package');a.add_argument('--source',required=True);a.add_argument('--sha',required=True);a.add_argument('--output',required=True)
    a=sub.add_parser('restore');a.add_argument('--archive',required=True);a.add_argument('--sha',required=True);a.add_argument('--tree',required=True);a.add_argument('--digest',required=True);a.add_argument('--destination',required=True)
    a=p.parse_args()
    result=package(a.source,a.sha,a.output)if a.command=='package'else restore(a.archive,a.sha,a.tree,a.digest,a.destination)
    print(json.dumps(result,sort_keys=True))
if __name__=='__main__':main()
