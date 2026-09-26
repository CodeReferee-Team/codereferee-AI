#!/usr/bin/env bash
# 통합 브랜치(data/consolidate-2026-09-26)가 main에 머지된 뒤에만 실행한다.
# 68개 일일 배치 브랜치를 원격에서 삭제한다. 열려 있는 PR은 브랜치 삭제 시 자동으로 닫힌다.
# 되돌리기: 삭제 전 아래 커밋 해시로 브랜치를 다시 만들 수 있다(원격 GC 전까지만 가능).
set -euo pipefail
REMOTE="${1:-origin}"

# 브랜치 | 커밋 해시 (복구용 기록)
# data/dataset-batch-2026-07-20 | 6dfc7724fdf8
# data/dataset-batch-2026-07-21 | aeb7e890afa4
# data/dataset-batch-2026-07-22 | e9e46792a256
# data/dataset-batch-2026-07-23 | ac4878f99533
# data/dataset-batch-2026-07-24 | f5200501755b
# data/dataset-batch-2026-07-25 | 1e54d3270002
# data/dataset-batch-2026-07-26 | a3d5a77d46d1
# data/dataset-batch-2026-07-27 | f709d43723da
# data/dataset-batch-2026-07-28 | 3191d66b1757
# data/dataset-batch-2026-07-29 | 1378a00a0db8
# data/dataset-batch-2026-07-30 | e730ca4e787b
# data/dataset-batch-2026-07-31 | c79a5f935d57
# data/dataset-batch-2026-08-01 | 490774b4b713
# data/dataset-batch-2026-08-02 | 76669e55f560
# data/dataset-batch-2026-08-03 | 5d0afa80631d
# data/dataset-batch-2026-08-05 | a6452672e550
# data/dataset-batch-2026-08-06 | 4ed030a5e5d1
# data/dataset-batch-2026-08-07 | dc51159a66c0
# data/dataset-batch-2026-08-08 | b686ca0e74ed
# data/dataset-batch-2026-08-09 | a3544db63449
# data/dataset-batch-2026-08-10 | 608c9f1e2d22
# data/dataset-batch-2026-08-11 | 07b9fe68b222
# data/dataset-batch-2026-08-12 | f241bd8924d1
# data/dataset-batch-2026-08-13 | 7979fd771175
# data/dataset-batch-2026-08-14 | 03d042864edd
# data/dataset-batch-2026-08-15 | ca8257cadf03
# data/dataset-batch-2026-08-16 | 91dca9792a1d
# data/dataset-batch-2026-08-17 | c2dfb99384f8
# data/dataset-batch-2026-08-18 | eb359d820723
# data/dataset-batch-2026-08-19 | 51cc78d41d14
# data/dataset-batch-2026-08-20 | 6e300995375a
# data/dataset-batch-2026-08-21 | 20fdda29804a
# data/dataset-batch-2026-08-22 | 4286f35058a0
# data/dataset-batch-2026-08-23 | cc3756dc7700
# data/dataset-batch-2026-08-24 | 0e830ed9aeee
# data/dataset-batch-2026-08-25 | 1469a45e03b2
# data/dataset-batch-2026-08-26 | 6598b5fb165a
# data/dataset-batch-2026-08-27 | f4799727c41c
# data/dataset-batch-2026-08-28 | ff3f949aa1ee
# data/dataset-batch-2026-08-29 | 42383f422b84
# data/dataset-batch-2026-08-30 | 65e29a00463f
# data/dataset-batch-2026-08-31 | 68aa0f58c633
# data/dataset-batch-2026-09-01 | f1b71c3f4611
# data/dataset-batch-2026-09-02 | 38018d4b3a18
# data/dataset-batch-2026-09-03 | 9df61630b2bc
# data/dataset-batch-2026-09-04 | ca99376ce73b
# data/dataset-batch-2026-09-05 | d4cddd474d0b
# data/dataset-batch-2026-09-06 | ba71c56465b9
# data/dataset-batch-2026-09-07 | c2e00822af9c
# data/dataset-batch-2026-09-08 | db9270b8217a
# data/dataset-batch-2026-09-09 | af09e22f5763
# data/dataset-batch-2026-09-10 | ce4a8b022396
# data/dataset-batch-2026-09-11 | cac38d9227a1
# data/dataset-batch-2026-09-12 | b3f72303f373
# data/dataset-batch-2026-09-13 | 0d49f6a90b3a
# data/dataset-batch-2026-09-14 | cbcc6a8bb938
# data/dataset-batch-2026-09-15 | 5e3201f0ce5f
# data/dataset-batch-2026-09-16 | 9760f8582267
# data/dataset-batch-2026-09-17 | 89a664253884
# data/dataset-batch-2026-09-18 | 01ced5ec9364
# data/dataset-batch-2026-09-19 | a715b5e193c3
# data/dataset-batch-2026-09-20 | 8b1030479c98
# data/dataset-batch-2026-09-21 | b35735e77fce
# data/dataset-batch-2026-09-22 | 854abcf77ff1
# data/dataset-batch-2026-09-23 | 284eedc3e0ad
# data/dataset-batch-2026-09-24 | fdcea95c8930
# data/dataset-batch-2026-09-25 | 428ce16390d4
# data/dataset-batch-2026-09-26 | 3c03521a6f6e

BRANCHES=(
  "data/dataset-batch-2026-07-20"
  "data/dataset-batch-2026-07-21"
  "data/dataset-batch-2026-07-22"
  "data/dataset-batch-2026-07-23"
  "data/dataset-batch-2026-07-24"
  "data/dataset-batch-2026-07-25"
  "data/dataset-batch-2026-07-26"
  "data/dataset-batch-2026-07-27"
  "data/dataset-batch-2026-07-28"
  "data/dataset-batch-2026-07-29"
  "data/dataset-batch-2026-07-30"
  "data/dataset-batch-2026-07-31"
  "data/dataset-batch-2026-08-01"
  "data/dataset-batch-2026-08-02"
  "data/dataset-batch-2026-08-03"
  "data/dataset-batch-2026-08-05"
  "data/dataset-batch-2026-08-06"
  "data/dataset-batch-2026-08-07"
  "data/dataset-batch-2026-08-08"
  "data/dataset-batch-2026-08-09"
  "data/dataset-batch-2026-08-10"
  "data/dataset-batch-2026-08-11"
  "data/dataset-batch-2026-08-12"
  "data/dataset-batch-2026-08-13"
  "data/dataset-batch-2026-08-14"
  "data/dataset-batch-2026-08-15"
  "data/dataset-batch-2026-08-16"
  "data/dataset-batch-2026-08-17"
  "data/dataset-batch-2026-08-18"
  "data/dataset-batch-2026-08-19"
  "data/dataset-batch-2026-08-20"
  "data/dataset-batch-2026-08-21"
  "data/dataset-batch-2026-08-22"
  "data/dataset-batch-2026-08-23"
  "data/dataset-batch-2026-08-24"
  "data/dataset-batch-2026-08-25"
  "data/dataset-batch-2026-08-26"
  "data/dataset-batch-2026-08-27"
  "data/dataset-batch-2026-08-28"
  "data/dataset-batch-2026-08-29"
  "data/dataset-batch-2026-08-30"
  "data/dataset-batch-2026-08-31"
  "data/dataset-batch-2026-09-01"
  "data/dataset-batch-2026-09-02"
  "data/dataset-batch-2026-09-03"
  "data/dataset-batch-2026-09-04"
  "data/dataset-batch-2026-09-05"
  "data/dataset-batch-2026-09-06"
  "data/dataset-batch-2026-09-07"
  "data/dataset-batch-2026-09-08"
  "data/dataset-batch-2026-09-09"
  "data/dataset-batch-2026-09-10"
  "data/dataset-batch-2026-09-11"
  "data/dataset-batch-2026-09-12"
  "data/dataset-batch-2026-09-13"
  "data/dataset-batch-2026-09-14"
  "data/dataset-batch-2026-09-15"
  "data/dataset-batch-2026-09-16"
  "data/dataset-batch-2026-09-17"
  "data/dataset-batch-2026-09-18"
  "data/dataset-batch-2026-09-19"
  "data/dataset-batch-2026-09-20"
  "data/dataset-batch-2026-09-21"
  "data/dataset-batch-2026-09-22"
  "data/dataset-batch-2026-09-23"
  "data/dataset-batch-2026-09-24"
  "data/dataset-batch-2026-09-25"
  "data/dataset-batch-2026-09-26"
)

printf "삭제 대상 %d개 브랜치\n" "${#BRANCHES[@]}"
read -r -p "정말 삭제할까요? (yes 입력) " ans
[ "$ans" = "yes" ] || { echo "취소됨"; exit 1; }
for b in "${BRANCHES[@]}"; do git push "$REMOTE" --delete "$b"; done
