#!/bin/bash
set -euo pipefail

BASE='http://tenholes.com'
UA='Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/126 Safari/537.36'
ROOT='output/all_tracks'
WORK="$ROOT/.work"
CONCURRENCY=${TENHOLES_CONCURRENCY:-12}

read -rs -p 'ten_auth1 cookie value: ' TOKEN
printf '\n'
mkdir -p "$WORK/lists" "$WORK/details" "$ROOT"

page=1
if [ ! -s "$WORK/detail_urls.txt" ]; then
  : > "$WORK/detail_urls.txt"
  while :; do
  html="$WORK/lists/page-$page.html"
  curl --fail --silent --show-error --location --retry 3 --retry-delay 1 \
    --connect-timeout 10 --max-time 45 --user-agent "$UA" \
    --cookie "ten_auth1=$TOKEN" \
    --output "$html.part" "$BASE/tabs/search?page=$page"
  mv "$html.part" "$html"
  grep -oE 'href="/tabs/view\?id=[0-9]+"' "$html" \
    | sed -E 's/^href="/http:\/\/tenholes.com/; s/"$//' >> "$WORK/detail_urls.txt"
  count=$(grep -c '/tabs/view?id=' "$html" || true)
  printf '列表页 %d: %d 首\n' "$page" "$count"
  if ! grep -qE 'class="next"[^>]*>.*href="/tabs/search\?page=' "$html"; then
    break
  fi
  page=$((page + 1))
    sleep 0.25
  done
  sort -u "$WORK/detail_urls.txt" -o "$WORK/detail_urls.txt"
fi

export TOKEN UA WORK
xargs -P "$CONCURRENCY" -n 1 sh -c '
  url="$1"; id=${url##*=}; out="$WORK/details/$id.html"
  curl --fail --silent --show-error --location --retry 3 --retry-delay 1 \
    --connect-timeout 10 --max-time 45 --user-agent "$UA" \
    --cookie "ten_auth1=$TOKEN" --output "$out.part" "$url" && mv "$out.part" "$out"
' sh < "$WORK/detail_urls.txt"

ruby - "$WORK" "$ROOT" <<'RUBY'
require 'cgi'
work, root = ARGV
File.write(File.join(root, 'index.tsv'), "id\ttitle\tpage_url\n")
File.write(File.join(work, 'resources.tsv'), '')
Dir[File.join(work, 'details', '*.html')].sort_by { |p| File.basename(p).to_i }.each do |path|
  id = File.basename(path, '.html')
  html = File.read(path)
  title = CGI.unescapeHTML(html[/<div class="mt-tit">.*?<p>(.*?)<\/p>/m, 1].to_s.gsub(/<[^>]+>/, '').strip)
  File.open(File.join(root, 'index.tsv'), 'a') { |f| f.puts [id, title.gsub(/[\t\r\n]+/, ' '), "http://tenholes.com/tabs/view?id=#{id}"].join("\t") }
  dir = File.join(root, id)
  Dir.mkdir(dir) unless Dir.exist?(dir)
  File.write(File.join(dir, 'title.txt'), title + "\n")

  sections = html.scan(/<div class="mt-con">(.*?)<\/div>/m).flatten
  sections.each_with_index do |section, section_index|
    section.scan(/<img[^>]+src=["']([^"']+)["']/i).flatten.each_with_index do |url, image_index|
      url = CGI.unescapeHTML(url)
      uri_path = url.split('?', 2).first
      ext = File.extname(uri_path)
      ext = '.jpg' if ext.empty?
      kind = section_index.zero? ? 'score' : 'scale'
      number = image_index + 1
      dest = File.join(dir, "#{kind}_#{number}#{ext}")
      File.open(File.join(work, 'resources.tsv'), 'a') { |f| f.puts [url, dest, id, kind].join("\t") }
    end
  end
  [['audioSrc', 'demo.mp3'], ['audioBzSrc', 'accompaniment.mp3']].each do |field, name|
    url = html[/id=["']#{field}["'][^>]*value=["']([^"']+)["']/i, 1]
    next if url.nil? || url.empty?
    File.open(File.join(work, 'resources.tsv'), 'a') { |f| f.puts [CGI.unescapeHTML(url), File.join(dir, name), id, 'mp3'].join("\t") }
  end
end
RUBY

: > "$ROOT/failures.tsv"
export ROOT
xargs -P "$CONCURRENCY" -n 4 sh -c '
  url="$1"; dest="$2"; id="$3"; kind="$4"
  if [ -s "$dest" ]; then exit 0; fi
  if curl --fail --silent --show-error --location --retry 3 --retry-delay 2 \
      --continue-at - \
      --connect-timeout 10 --max-time 600 --user-agent "$UA" \
      --cookie "ten_auth1=$TOKEN" --output "$dest.part" "$url"; then
    mv "$dest.part" "$dest"
  else
    printf "%s\t%s\t%s\t%s\n" "$id" "$kind" "$url" "$dest" >> "$ROOT/failures.tsv"
  fi
' sh < "$WORK/resources.tsv"

tracks=$(tail -n +2 "$ROOT/index.tsv" | wc -l | tr -d ' ')
resources=$(wc -l < "$WORK/resources.tsv" | tr -d ' ')
failures=$(wc -l < "$ROOT/failures.tsv" | tr -d ' ')
printf '完成: 曲目=%s, 资源=%s, 失败=%s, 目录=%s\n' "$tracks" "$resources" "$failures" "$ROOT"
