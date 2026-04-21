#!/usr/bin/env node
/* thx! https://zenn.dev/bltsdc/articles/cf65b441c62908 */

/**
 * docs/ 配下の設計書を GitHub Wiki に同期するスクリプト
 * 
 * このスクリプトはGitHub Actions環境でのみ実行可能です。
 * ローカル環境からの実行は許可されていません。
 */

const fs = require('fs');
const path = require('path');
const { execSync } = require('child_process');

// ---- 環境変数の検証（main 実行時のみ呼ぶ） ----
function validateEnv() {
  const GITHUB_TOKEN = process.env.GITHUB_TOKEN;
  const GITHUB_REPO = process.env.GITHUB_REPO || process.env.GITHUB_REPOSITORY;
  const GITHUB_ACTIONS = process.env.GITHUB_ACTIONS === 'true';

  if (!GITHUB_ACTIONS) {
    console.error('❌ このスクリプトはGitHub Actions環境でのみ実行できます');
    console.error('   ローカル環境からの実行は許可されていません');
    console.error('   Wiki同期はGitHub Actionsから自動的に実行されます');
    process.exit(1);
  }

  if (!GITHUB_TOKEN) {
    console.error('❌ GITHUB_TOKEN 環境変数が設定されていません');
    process.exit(1);
  }

  if (!GITHUB_REPO) {
    console.error('❌ GITHUB_REPO 環境変数が設定されていません');
    console.error('   例: GITHUB_REPO=owner/repo');
    process.exit(1);
  }

  const [owner, repo] = GITHUB_REPO.split('/');
  if (!owner || !repo) {
    console.error('❌ GITHUB_REPO の形式が正しくありません');
    console.error('   例: GITHUB_REPO=owner/repo');
    process.exit(1);
  }

  // 同期先ブランチ名（main repo の現在のブランチ）。リンク先絶対 URL に使う。
  const REPO_BRANCH = process.env.GITHUB_REF_NAME || 'main';

  return { GITHUB_TOKEN, GITHUB_REPO, owner, repo, REPO_BRANCH };
}

// ここは実際の構成に合わせて変更してください
const DOCS_DIR = path.join(process.cwd(), 'docs', 'wiki');

// Wiki にのみ残すべきページ（削除対象外）
// [private] で始まるページは自動的に保護されます
const PROTECTED_WIKI_PAGES = [];

/**
 * docs/wiki/ 配下の Markdown ファイルを再帰的に取得
 */
function getMarkdownFiles(dir, basePath = '') {
  const files = [];
  const entries = fs.readdirSync(dir, { withFileTypes: true });

  for (const entry of entries) {
    const fullPath = path.join(dir, entry.name);
    const relativePath = path.join(basePath, entry.name);

    if (entry.isDirectory()) {
      // サブディレクトリも含める
      files.push(...getMarkdownFiles(fullPath, relativePath));
    } else if (entry.isFile() && entry.name.endsWith('.md')) {
      files.push({
        filePath: fullPath,
        relativePath: relativePath,
        wikiTitle: getWikiTitle(relativePath),
      });
    }
  }

  return files;
}

/**
 * ファイルパスから Wiki ページタイトルを生成
 */
function getWikiTitle(relativePath) {
  // ファイル名から拡張子を除去
  const nameWithoutExt = relativePath.replace(/\.md$/, '');

  // パス区切りをハイフンに変換（必要に応じて調整）
  // 例: "01_要件定義書" -> "01_要件定義書"
  // 例: "01_要件定義書/顧客要件" -> "01_要件定義書-顧客要件"
  return nameWithoutExt.replace(/\//g, '-');
}

/**
 * Markdown content の中のリンクを GitHub Wiki 仕様に変換する。
 *
 * 変換ルール:
 *   1. 同階層の Wiki ページ link `[text](Foo.md)` → `[text](Foo)`
 *      ─ Wiki は拡張子なしページ名でアクセスする。
 *   2. アンカー付き `[text](Foo.md#bar)` → `[text](Foo#bar)`
 *   3. 親ディレクトリ参照 `[text](../foo/bar.md)` や `[text](../../code.py)`
 *      → `[text](https://github.com/<owner>/<repo>/blob/<branch>/<resolved-path>)`
 *      ─ Wiki から repo の他ファイルへ飛ぶには絶対 URL が必要。
 *   4. 外部 URL / メール / アンカーのみ / `[[...]]` (Wiki ネイティブ) → そのまま
 *   5. コードブロック (``` または ~~~ で囲まれた範囲) 内のリンクは変換しない
 *      ─ サンプルコードを壊さないため
 *
 * 画像参照 `![alt](path)` も同様に処理する（同じ regex でカバー）。
 */
function transformWikiLinks(content, owner, repo, branch = 'main') {
  const baseUrl = `https://github.com/${owner}/${repo}/blob/${branch}`;
  const lines = content.split('\n');
  let inFence = false;          // ``` or ~~~ コードフェンスの中か
  let fenceMarker = '';

  // [text](target) と ![alt](target) の両方を拾う
  const linkRe = /(!?)\[([^\]]*)\]\(([^)]+)\)/g;

  return lines.map(line => {
    // フェンス開閉判定
    const fenceMatch = line.match(/^(\s*)(```+|~~~+)/);
    if (fenceMatch) {
      const marker = fenceMatch[2][0]; // ` or ~
      if (!inFence) {
        inFence = true;
        fenceMarker = marker;
      } else if (marker === fenceMarker) {
        inFence = false;
        fenceMarker = '';
      }
      return line;
    }
    if (inFence) return line;

    return line.replace(linkRe, (match, bang, text, target) => {
      // インラインコード内 `...[x](y)...` は素朴には判定が難しい。
      // ここでは backtick 数を数えて、リンク全体が backtick の内側ならスキップする近似を行う。
      const before = line.slice(0, line.indexOf(match));
      const ticksBefore = (before.match(/`/g) || []).length;
      if (ticksBefore % 2 === 1) return match; // インラインコード内

      const trimmed = target.trim();

      // 1. 外部 URL / メール / 純粋なアンカー / Wiki ネイティブリンクは触らない
      if (/^(https?:|mailto:|tel:|ftp:|#)/.test(trimmed)) return match;

      // 2. 同階層の .md (任意で #anchor 付き) → Wiki ページ
      const sameLevelMd = trimmed.match(/^(?:\.\/)?([^\/]+?)\.md(#.*)?$/);
      if (sameLevelMd) {
        const wikiName = sameLevelMd[1];
        const anchor = sameLevelMd[2] || '';
        return `${bang}[${text}](${wikiName}${anchor})`;
      }

      // 3. ../ で repo 内の他ファイルを指している → 絶対 GitHub URL
      if (trimmed.startsWith('../') || trimmed.startsWith('./')) {
        // docs/wiki/<file> の視点で resolve するために、
        // 先頭の ./ や ../ を取り除いて repo root からの相対パスに変換する。
        const segments = trimmed.split('/');
        // docs/wiki/ から見た相対なので、上位への ".." はそれぞれ docs/wiki, docs と一段ずつ上がる。
        // 既知の親ディレクトリ chain ("docs/wiki" → "docs" → repo root)。
        // ここでは sync は docs/wiki/ から行うので、`../` 1 つ = docs/、`../../` 2 つ = repo root。
        let upCount = 0;
        let i = 0;
        while (i < segments.length && (segments[i] === '..' || segments[i] === '.')) {
          if (segments[i] === '..') upCount++;
          i++;
        }
        const tail = segments.slice(i).join('/');
        const docsWikiAncestors = ['docs/wiki', 'docs', '']; // index 0=ここ, 1=親, 2=祖父母
        // upCount=1 → docs/, upCount=2 → repo root, upCount>=3 → repo root に丸める
        const baseInRepo = docsWikiAncestors[Math.min(upCount, docsWikiAncestors.length - 1)] || '';
        const absPath = baseInRepo ? `${baseInRepo}/${tail}` : tail;
        return `${bang}[${text}](${baseUrl}/${absPath})`;
      }

      // 4. その他（裸の Foo や Foo/bar.png 等）は触らない
      return match;
    });
  }).join('\n');
}

// ローカルテスト用に export（GitHub Actions の本番実行には影響しない）
module.exports = { transformWikiLinks, getWikiTitle };

/**
 * Wiki ページを作成または更新（Gitリポジトリ経由）
 */
function createOrUpdateWikiPage(title, content, wikiDir) {
  const fileName = `${title}.md`;
  const filePath = path.join(wikiDir, fileName);

  // ファイルが既に存在するか確認
  const exists = fs.existsSync(filePath);
  
  // ファイルを書き込み
  fs.writeFileSync(filePath, content, 'utf-8');
  
  // Gitに追加
  execSync(`cd "${wikiDir}" && git add "${fileName}"`, { stdio: 'pipe' });
  
  if (exists) {
    console.log(`✅ 更新: ${title}`);
  } else {
    console.log(`✨ 作成: ${title}`);
  }
}

/**
 * Wiki リポジトリからすべてのページを取得（Git操作のみ）
 */
function getAllWikiPages(wikiDir) {
  const pages = [];
  
  if (!fs.existsSync(wikiDir)) {
    return [];
  }

  const files = fs.readdirSync(wikiDir);
  for (const file of files) {
    if (file.endsWith('.md')) {
      const title = file.replace(/\.md$/, '');
      // システムページは除外
      if (title !== '_Sidebar' && title !== 'Home') {
        pages.push({
          title: title,
          fileName: file,
        });
      }
    }
  }

  return pages;
}

/**
 * ページタイトルが保護対象かどうかを判定
 */
function isProtectedPage(title) {
  // [private] で始まるページは保護対象
  if (title.startsWith('[private]')) {
    return true;
  }
  
  // 明示的に指定された保護対象ページ
  return PROTECTED_WIKI_PAGES.some(protectedPage => {
    // 完全一致または、スラッシュ/ハイフンの違いを考慮
    return title === protectedPage || 
           title === protectedPage.replace(/\//g, '-') ||
           title === protectedPage.replace(/-/g, '/');
  });
}

/**
 * Wiki ページを削除（Git操作のみ）
 */
function deleteWikiPages(wikiDir, pagesToDelete) {
  if (pagesToDelete.length === 0) {
    return 0;
  }

  let deletedCount = 0;
  for (const pageTitle of pagesToDelete) {
    const fileName = `${pageTitle}.md`;
    const filePath = path.join(wikiDir, fileName);
    
    if (fs.existsSync(filePath)) {
      execSync(`cd "${wikiDir}" && git rm "${fileName}"`, { stdio: 'pipe' });
      console.log(`🗑️  削除: ${pageTitle}`);
      deletedCount++;
    }
  }

  return deletedCount;
}

/**
 * メイン処理
 */
function main() {
  const { GITHUB_TOKEN, GITHUB_REPO, owner, repo, REPO_BRANCH } = validateEnv();
  console.log(`📚 ${GITHUB_REPO} の Wiki に同期を開始します...\n`);

  // docs/wiki/ ディレクトリの存在確認
  if (!fs.existsSync(DOCS_DIR)) {
    console.error(`❌ docs/wiki/ ディレクトリが見つかりません: ${DOCS_DIR}`);
    process.exit(1);
  }

  // Markdown ファイルの取得
  const files = getMarkdownFiles(DOCS_DIR);
  console.log(`📄 ${files.length} 個の Markdown ファイルが見つかりました\n`);

  // Wikiリポジトリをクローン
  const wikiDir = path.join(process.cwd(), '.wiki-temp');
  // GitHub Actions環境では、URLに直接トークンを埋め込む（ユーザー名に x-access-token を利用）
  const encodedToken = encodeURIComponent(GITHUB_TOKEN);
  const wikiRepoUrl = `https://x-access-token:${encodedToken}@github.com/${owner}/${repo}.wiki.git`;
  const gitUserName = process.env.GITHUB_ACTOR || 'github-actions[bot]';
  const gitUserEmail = process.env.GIT_COMMIT_EMAIL || `${gitUserName}@users.noreply.github.com`;
 
  try {
    // GitHub Actions環境でのGit認証設定
    // GIT_TERMINAL_PROMPTを0に設定してパスワードプロンプトを無効化
    const gitEnv = {
      ...process.env,
      GIT_TERMINAL_PROMPT: '0',
      GIT_ASKPASS: 'echo',
    };
 
    if (!fs.existsSync(wikiDir)) {
      console.log('📥 Wiki リポジトリをクローン中...\n');
      execSync(`git clone "${wikiRepoUrl}" "${wikiDir}"`, { 
        stdio: 'inherit',
        env: gitEnv
      });
    } else {
      execSync(`cd "${wikiDir}" && git pull origin master`, { 
        stdio: 'pipe',
        env: gitEnv
      });
    }
 
    // リモートURLを認証付きURLに更新（既存クローン対策）
    execSync(`cd "${wikiDir}" && git remote set-url origin "${wikiRepoUrl}"`, {
      stdio: 'pipe',
      env: gitEnv,
    });

    // Gitユーザー情報を設定
    execSync(`cd "${wikiDir}" && git config user.name "${gitUserName}"`, {
      stdio: 'pipe',
      env: gitEnv,
    });
    execSync(`cd "${wikiDir}" && git config user.email "${gitUserEmail}"`, {
      stdio: 'pipe',
      env: gitEnv,
    });

    // Wiki リポジトリから既存のページを取得
    const wikiPages = getAllWikiPages(wikiDir);
    const docsWikiTitles = new Set(files.map(f => f.wikiTitle));
    
    // 保護対象ページを取得（サイドバーに含めるため）
    const protectedPages = wikiPages
      .filter(page => {
        const title = page.title;
        return isProtectedPage(title);
      })
      .map(page => ({
        title: page.title,
        wikiTitle: page.title,
        fileName: page.fileName,
      }));

    // すべてのファイルを追加（リンクを Wiki 仕様に変換してから書き込む）
    for (const file of files) {
      const raw = fs.readFileSync(file.filePath, 'utf-8');
      const transformed = transformWikiLinks(raw, owner, repo, REPO_BRANCH);
      createOrUpdateWikiPage(file.wikiTitle, transformed, wikiDir);
    }

    // サイドバーの扱い:
    //   - docs/wiki/_Sidebar.md がソースにあれば、上の for-loop で transformWikiLinks
    //     を通して既に書き込み済み（カテゴリ分け・絵文字・手動キュレーションを尊重）
    //   - 無ければフラットな自動生成リストにフォールバックし、保護ページも含める
    const hasCustomSidebar = files.some(f => f.wikiTitle === '_Sidebar');
    if (hasCustomSidebar) {
      console.log('\n📑 docs/wiki/_Sidebar.md を尊重します（自動生成スキップ）');
      // 保護ページが指定されている場合は警告だけ出す
      if (protectedPages.length > 0) {
        console.log(`   ⚠️  保護ページ ${protectedPages.length} 個はサイドバーに自動追加されません`);
        console.log(`      docs/wiki/_Sidebar.md に手動で追加してください:`);
        protectedPages.forEach(p => console.log(`        - [[${p.wikiTitle}|${p.wikiTitle}]]`));
      }
    } else {
      const sidebarContent = `# 目次

${files
  .filter(f => !f.relativePath?.includes('wiki-backup'))
  .map(f => `- [[${f.wikiTitle}|${f.wikiTitle}]]`)
  .join('\n')}

${protectedPages.length > 0 ? `## Wiki 専用ページ\n\n${protectedPages.map(p => `- [[${p.wikiTitle}|${p.wikiTitle}]]`).join('\n')}\n` : ''}
`;
      createOrUpdateWikiPage('_Sidebar', sidebarContent, wikiDir);
    }

    // 削除対象のページを処理
    const pagesToDelete = wikiPages
      .filter(page => {
        const title = page.title;
        // 保護対象ページは削除しない
        if (isProtectedPage(title)) {
          return false;
        }
        // docs/wiki/ に存在しないページのみ削除対象
        return !docsWikiTitles.has(title);
      })
      .map(page => page.title);

    // 保護対象ページの確認ログ
    const allProtectedPages = wikiPages.filter(page => isProtectedPage(page.title));
    if (allProtectedPages.length > 0) {
      console.log(`\n🔒 保護された Wiki ページ（削除対象外）: ${allProtectedPages.length} 個`);
      allProtectedPages.forEach(page => console.log(`   - ${page.title}`));
    }

    if (pagesToDelete.length > 0) {
      console.log(`\n🗑️  削除対象の Wiki ページ: ${pagesToDelete.length} 個`);
      pagesToDelete.forEach(title => console.log(`   - ${title}`));
      deleteWikiPages(wikiDir, pagesToDelete);
    } else {
      console.log('\n✅ 削除対象のページはありませんでした');
    }

    // 変更があるか確認してコミット・プッシュ
    try {
      execSync(`cd "${wikiDir}" && git diff --cached --quiet`, { stdio: 'pipe' });
      console.log('\n✅ 変更はありませんでした');
    } catch {
      // 変更がある場合はコミット・プッシュ
      console.log('\n💾 変更をコミット中...');
      execSync(`cd "${wikiDir}" && git commit -m "Sync docs/wiki to GitHub Wiki"`, { stdio: 'inherit' });
      
      console.log('📤 変更をプッシュ中...');
      execSync(`cd "${wikiDir}" && git push origin master`, { 
        stdio: 'inherit',
        env: gitEnv
      });
      
      console.log('\n✅ Wikiへの同期が完了しました！');
    }
  } finally {
    // 一時ディレクトリをクリーンアップ
    if (fs.existsSync(wikiDir)) {
      execSync(`rm -rf "${wikiDir}"`, { stdio: 'pipe' });
    }
  }
}

if (require.main === module) {
  try {
    main();
  } catch (error) {
    console.error('\n❌ エラーが発生しました:', error);
    process.exit(1);
  }
}