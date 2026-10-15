#!/bin/bash
set -euo pipefail

BASE='http://tenholes.com'
UA='Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/126 Safari/537.36'
ROOT='output/new_first_3_pages'
WORK="$ROOT/.work"
CONCURRENCY=${TENHOLES_CONCURRENCY:-4}

read -rs -p 'ten_auth1 cookie value: ' TOKEN
printf '\n'
mkdir -p "$WORK/lists" "$WORK/details" "$ROOT"
: > "$WORK/detail_urls.txt"

for page in 1 2 3; do
  html="$WORK/lists/page-$page.html"
  if [ ! -s "$html" ]; then
    curl --fail --silent --show-error --location --retry 3 --retry-delay 1 \
      --connect-timeout 10 --max-time 45 --user-agent "$UA" \
      --cookie "ten_auth1=$TOKEN" \
      --output "$html.part" "$BASE/tabs/search?action=new&page=$page"
    mv "$html.part" "$html"
  fi
  grep -oE 'href="/tabs/view\?id=[0-9]+"' "$html" \
    | sed -E 's/^href="/http:\/\/tenholes.com/; s/"$//' >> "$WORK/detail_urls.txt"
  count=$(grep -c '/tabs/view?id=' "$html" || true)
  printf '列表页 %d: %d 首\n' "$page" "$count"
  sleep 0.25
done

awk '!seen[$0]++' "$WORK/detail_urls.txt" > "$WORK/detail_urls.unique"
mv "$WORK/detail_urls.unique" "$WORK/detail_urls.txt"

export TOKEN UA WORK
xargs -P "$CONCURRENCY" -n 1 sh -c '
  url="$1"; id=${url##*=}; out="$WORK/details/$id.html"
  [ -s "$out" ] && exit 0
  curl --fail --silent --show-error --location --retry 3 --retry-delay 1 \
    --connect-timeout 10 --max-time 45 --user-agent "$UA" \
    --cookie "ten_auth1=$TOKEN" --output "$out.part" "$url" \
    && mv "$out.part" "$out"
' sh < "$WORK/detail_urls.txt"

ruby - "$WORK" "$ROOT" <<'RUBY'
require 'cgi'

work, root = ARGV
details = File.readlines(File.join(work, 'detail_urls.txt'), chomp: true).map do |url|
  [url[/[?&]id=(\d+)/, 1], url]
end

records = details.map do |id, url|
  html = File.read(File.join(work, 'details', "#{id}.html"))
  title = CGI.unescapeHTML(
    html[/<div class="mt-tit">.*?<p>(.*?)<\/p>/m, 1].to_s
      .gsub(/<[^>]+>/, '').strip
  )
  raise "详情页没有标题: #{url}" if title.empty?

  score_section = html[/<div class="mt-con">(.*?)<\/div>/m, 1].to_s
  score_urls = score_section.scan(/<img[^>]+src=["']([^"']+)["']/i).flatten
    .map { |src| CGI.unescapeHTML(src) }
  raise "详情页没有曲谱图: #{url}" if score_urls.empty?
  audio_urls = {
    'demo.mp3' => html[/id=["']audioSrc["'][^>]*value=["']([^"']+)["']/i, 1],
    'accompaniment.mp3' => html[/id=["']audioBzSrc["'][^>]*value=["']([^"']+)["']/i, 1]
  }.transform_values { |src| src.nil? ? nil : CGI.unescapeHTML(src) }
  [id, title, url, score_urls, audio_urls]
end

sanitize = lambda do |title|
  title.gsub('/', '／').gsub(':', '：').gsub(/[\x00-\x1f]/, '').sub(/[. ]+\z/, '')
end
groups = records.group_by { |_, title, _, _, _| sanitize.call(title).downcase }

File.write(File.join(root, 'index.tsv'), "id\ttitle\tdirectory\tpage_url\n")
File.write(File.join(work, 'scores.tsv'), '')
File.write(File.join(work, 'scores.args'), '', mode: 'wb')
File.write(File.join(work, 'audio.tsv'), '')
File.write(File.join(work, 'audio.args'), '', mode: 'wb')
File.write(File.join(root, 'missing-audio.tsv'), "id\ttitle\ttype\treason\n")

records.each do |id, title, page_url, score_urls, audio_urls|
  base = sanitize.call(title)
  directory = groups.fetch(base.downcase).length > 1 ? "#{base}（#{id}）" : base
  dir = File.join(root, directory)
  Dir.mkdir(dir) unless Dir.exist?(dir)
  File.write(File.join(dir, 'title.txt'), title + "\n")
  File.open(File.join(root, 'index.tsv'), 'a') do |f|
    f.puts [id, title.gsub(/[\t\r\n]+/, ' '), directory, page_url].join("\t")
  end

  score_urls.each_with_index do |score_url, index|
    extension = File.extname(score_url.split('?', 2).first)
    extension = '.jpg' if extension.empty?
    destination = File.join(dir, "score_#{index + 1}#{extension}")
    File.open(File.join(work, 'scores.tsv'), 'a') do |f|
      f.puts [score_url, destination, id, title].join("\t")
    end
    File.open(File.join(work, 'scores.args'), 'ab') do |f|
      [score_url, destination, id].each { |value| f.write(value + "\0") }
    end
  end

  audio_urls.each do |filename, audio_url|
    if audio_url.nil? || audio_url.empty? || audio_url == 'http://authcdn.tenholes.com/'
      File.open(File.join(root, 'missing-audio.tsv'), 'a') do |f|
        f.puts [id, title.gsub(/[\t\r\n]+/, ' '), filename, 'source URL has no file path'].join("\t")
      end
      next
    end
    destination = File.join(dir, filename)
    File.open(File.join(work, 'audio.tsv'), 'a') do |f|
      f.puts [audio_url, destination, id, filename].join("\t")
    end
    File.open(File.join(work, 'audio.args'), 'ab') do |f|
      [audio_url, destination, id, filename].each { |value| f.write(value + "\0") }
    end
  end
end
RUBY

: > "$ROOT/failures.tsv"
export ROOT
xargs -0 -P "$CONCURRENCY" -n 3 sh -c '
  url="$1"; destination="$2"; id="$3"
  [ -s "$destination" ] && exit 0
  if curl --fail --silent --show-error --location --retry 3 --retry-delay 2 \
      --connect-timeout 10 --max-time 180 --user-agent "$UA" \
      --cookie "ten_auth1=$TOKEN" --output "$destination.part" "$url"; then
    mv "$destination.part" "$destination"
  else
    rm -f "$destination.part"
    printf "%s\t%s\t%s\n" "$id" "$url" "$destination" >> "$ROOT/failures.tsv"
  fi
' sh < "$WORK/scores.args"

xargs -0 -P "$CONCURRENCY" -n 4 sh -c '
  url="$1"; destination="$2"; id="$3"; type="$4"
  [ -s "$destination" ] && exit 0
  if curl --fail --silent --show-error --location --retry 4 --retry-delay 2 \
      --continue-at - --connect-timeout 10 --max-time 600 --user-agent "$UA" \
      --cookie "ten_auth1=$TOKEN" --output "$destination.part" "$url"; then
    mv "$destination.part" "$destination"
  else
    printf "%s\t%s\t%s\t%s\n" "$id" "$type" "$url" "$destination" >> "$ROOT/failures.tsv"
  fi
' sh < "$WORK/audio.args"

tracks=$(tail -n +2 "$ROOT/index.tsv" | wc -l | tr -d ' ')
scores=$(find "$ROOT" -mindepth 2 -maxdepth 2 -type f -name 'score_*' | wc -l | tr -d ' ')
audio=$(find "$ROOT" -mindepth 2 -maxdepth 2 -type f -name '*.mp3' | wc -l | tr -d ' ')
failures=$(wc -l < "$ROOT/failures.tsv" | tr -d ' ')
printf '完成: 曲目=%s, 曲谱图=%s, 音频=%s, 失败=%s, 目录=%s\n' "$tracks" "$scores" "$audio" "$failures" "$ROOT"
