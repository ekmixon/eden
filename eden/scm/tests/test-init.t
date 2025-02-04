#chg-compatible

This test tries to exercise the ssh functionality with a dummy script

(enable general delta early)

  $ setconfig format.usegeneraldelta=yes
  $ configure dummyssh

  $ checknewrepo()
  > {
  >    name=$1
  >    if [ -d "$name"/.hg/store ]; then
  >    echo store created
  >    fi
  >    if [ -f "$name"/.hg/00changelog.i ]; then
  >    echo 00changelog.i created
  >    fi
  >    cat "$name"/.hg/requires
  > }

creating 'local'

  $ hg init local
  $ checknewrepo local
  store created
  00changelog.i created
  dotencode
  fncache
  generaldelta
  lz4revlog
  revlogv1
  store
  treestate
  $ echo this > local/foo
  $ hg ci --cwd local -A -m "init"
  adding foo

test custom revlog chunk cache sizes

  $ hg --config format.chunkcachesize=0 log -R local -pv
  abort: revlog chunk cache size 0 is not greater than 0!
  [255]
  $ hg --config format.chunkcachesize=1023 log -R local -pv
  abort: revlog chunk cache size 1023 is not a power of 2!
  [255]
  $ hg --config format.chunkcachesize=1024 log -R local -pv
  commit:      08b9e9f63b32
  user:        test
  date:        Thu Jan 01 00:00:00 1970 +0000
  files:       foo
  description:
  init
  
  
  diff -r 000000000000 -r 08b9e9f63b32 foo
  --- /dev/null	Thu Jan 01 00:00:00 1970 +0000
  +++ b/foo	Thu Jan 01 00:00:00 1970 +0000
  @@ -0,0 +1,1 @@
  +this
  

creating repo with format.usefncache=false

  $ hg --config format.usefncache=false init old2
  $ checknewrepo old2
  store created
  00changelog.i created
  dotencode
  fncache
  generaldelta
  lz4revlog
  revlogv1
  store
  treestate

creating repo with format.dotencode=false

  $ hg --config format.dotencode=false init old3
  $ checknewrepo old3
  store created
  00changelog.i created
  dotencode
  fncache
  generaldelta
  lz4revlog
  revlogv1
  store
  treestate

creating repo with format.dotencode=false

  $ hg --config format.generaldelta=false --config format.usegeneraldelta=false init old4
  $ checknewrepo old4
  store created
  00changelog.i created
  dotencode
  fncache
  lz4revlog
  revlogv1
  store
  treestate

test failure

  $ hg init local
  abort: repository local already exists!
  [255]

init+push to remote2

  $ hg init ssh://user@dummy/remote2
  $ hg incoming -R remote2 local
  comparing with local
  commit:      08b9e9f63b32
  user:        test
  date:        Thu Jan 01 00:00:00 1970 +0000
  summary:     init
  

  $ hg push -R local ssh://user@dummy/remote2
  pushing to ssh://user@dummy/remote2
  searching for changes
  remote: adding changesets
  remote: adding manifests
  remote: adding file changes
  remote: added 1 changesets with 1 changes to 1 files

clone to remote1

  $ hg clone local ssh://user@dummy/remote1
  searching for changes
  remote: adding changesets
  remote: adding manifests
  remote: adding file changes
  remote: added 1 changesets with 1 changes to 1 files

init to existing repo

  $ hg clone local ssh://user@dummy/remotelf
  searching for changes
  remote: adding changesets
  remote: adding manifests
  remote: adding file changes
  remote: added 1 changesets with 1 changes to 1 files

  $ hg init ssh://user@dummy/remote1
  abort: repository remote1 already exists!
  abort: could not create remote repo!
  [255]

clone to existing repo

  $ hg clone local ssh://user@dummy/remote1
  abort: repository remote1 already exists!
  abort: could not create remote repo!
  [255]

output of dummyssh

  $ cat dummylog
  Got arguments 1:user@dummy 2:hg init remote2
  Got arguments 1:user@dummy 2:hg -R remote2 serve --stdio
  Got arguments 1:user@dummy 2:hg -R remote2 serve --stdio
  Got arguments 1:user@dummy 2:hg init remote1
  Got arguments 1:user@dummy 2:hg -R remote1 serve --stdio
  Got arguments 1:user@dummy 2:hg init remotelf
  Got arguments 1:user@dummy 2:hg -R remotelf serve --stdio
  Got arguments 1:user@dummy 2:hg init remote1
  Got arguments 1:user@dummy 2:hg init remote1

comparing repositories

  $ hg tip -q -R local
  08b9e9f63b32
  $ hg tip -q -R remote1
  08b9e9f63b32
  $ hg tip -q -R remote2
  08b9e9f63b32

check names for repositories (clashes with URL schemes, special chars)

  $ for i in bundle file hg http https old-http ssh static-http "with space"; do
  >   printf "hg init \"$i\"... "
  >   hg init "$i"
  >   test -d "$i" -a -d "$i/.hg" && echo "ok" || echo "failed"
  > done
  hg init "bundle"... ok
  hg init "file"... ok
  hg init "hg"... ok
  hg init "http"... ok
  hg init "https"... ok
  hg init "old-http"... ok
  hg init "ssh"... ok
  hg init "static-http"... ok
  hg init "with space"... ok
#if eol-in-paths
/* " " is not a valid name for a directory on Windows */
  $ hg init " "
  $ test -d " "
  $ test -d " /.hg"
#endif

creating 'local/sub/repo'

  $ hg init local/sub/repo
  $ checknewrepo local/sub/repo
  store created
  00changelog.i created
  dotencode
  fncache
  generaldelta
  lz4revlog
  revlogv1
  store
  treestate

prepare test of init of url configured from paths

  $ echo '[paths]' >> $HGRCPATH
  $ echo "somewhere = `pwd`/url from paths" >> $HGRCPATH
  $ echo "elsewhere = `pwd`/another paths url" >> $HGRCPATH

init should (for consistency with clone) expand the url

  $ hg init somewhere
  $ checknewrepo "url from paths"
  store created
  00changelog.i created
  dotencode
  fncache
  generaldelta
  lz4revlog
  revlogv1
  store
  treestate

verify that clone also expand urls

  $ hg clone somewhere elsewhere
  updating to branch default
  0 files updated, 0 files merged, 0 files removed, 0 files unresolved
  $ checknewrepo "another paths url"
  store created
  00changelog.i created
  dotencode
  fncache
  generaldelta
  lz4revlog
  revlogv1
  store
  treestate

clone bookmarks

  $ hg -R local bookmark test
  $ hg -R local bookmarks
   * test                      08b9e9f63b32
  $ hg clone local ssh://user@dummy/remote-bookmarks
  searching for changes
  exporting bookmark test
  remote: adding changesets
  remote: adding manifests
  remote: adding file changes
  remote: added 1 changesets with 1 changes to 1 files
  $ hg -R remote-bookmarks bookmarks
     test                      08b9e9f63b32
