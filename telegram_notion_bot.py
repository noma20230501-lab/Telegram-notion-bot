#!/usr/bin/env python3
"""
텔레그램 부동산 매물 -> 노션 자동 등록 봇
(여러 장 사진 앨범 지원 + 원본 수정 시 노션 자동 반영)
"""

import os
import re
import sys
import html
import asyncio
import logging
from datetime import datetime
from typing import Dict, List, Optional, Tuple
from pathlib import Path

from dotenv import load_dotenv
from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    filters,
    ContextTypes,
)
from notion_client import Client

# 로깅 설정
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# .env 파일 로드
load_dotenv()


class PropertyParser:
    """매물 정보 파싱 클래스"""

    @staticmethod
    def parse_property_info(
        text: str, skip_address: bool = False
    ) -> Dict[str, any]:
        """텔레그램 메시지에서 매물 정보 추출

        Args:
            text: 파싱할 텍스트
            skip_address: True이면 첫 줄을 주소로 처리하지 않음 (수정 모드)
        """

        # 4번 섹션 다중 줄 처리 (층별 면적/용도가 다음 줄에 이어지는 경우 합치기)
        text = PropertyParser._merge_section4_lines(text.strip())
        lines = text.strip().split("\n")
        data = {}

        start_idx = 0
        if not skip_address and lines:
            주소_line = lines[0].strip()
            data["주소"] = 주소_line
            
            # 매물 유형 감지: 괄호 안에 "복층" 또는 "통상가" 포함
            괄호_내용 = re.search(r'\(([^)]+)\)', 주소_line)
            if 괄호_내용:
                내용 = 괄호_내용.group(1)
                if "복층" in 내용:
                    data["매물_유형"] = "복층"
                elif "통상가" in 내용:
                    data["매물_유형"] = "통상가"
            
            # 소재지(구) 추출: 중구, 동구, 서구, 남구, 북구, 수성구, 달서구, 달성군
            구_match = re.search(r'(중구|동구|서구|남구|북구|수성구|달서구|달성군)', 주소_line)
            if 구_match:
                data["소재지_구"] = 구_match.group(1)
            
            # 임대 구분: "일부" 또는 "일부분"이 있으면 🌓일부
            if re.search(r'일부(?:분)?', 주소_line):
                data["임대_구분"] = "🌓일부"
            
            start_idx = 1

        special_notes = []
        in_special_section = False
        contact_idx = 0  # 연락처 인덱스 (0=대표, 1=추가1, 2=추가2)
        in_contacts = False  # 8번 연락처 섹션 여부

        for line in lines[start_idx:]:
            line = line.strip()
            if not line:
                continue

            if "특이사항" in line:
                in_special_section = True
                in_contacts = False
                # "특이사항+" → 추가 모드 플래그
                if "특이사항+" in line:
                    data["특이사항_추가"] = True
                    rest = line.split("특이사항+", 1)[1].strip()
                else:
                    rest = line.split("특이사항", 1)[1].strip()
                # 같은 줄에 내용이 있으면 바로 추가
                if rest:
                    special_notes.append(rest)
                continue

            if in_special_section:
                special_notes.append(line)
                continue

            # 번호 붙은 줄이면 연락처 섹션 해제 (8. 제외)
            is_numbered = re.match(r'^\d+\.', line)
            if is_numbered and not line.startswith("8."):
                in_contacts = False

            # 1. 보증금/월세/부가세
            if line.startswith("1."):
                content1 = re.sub(r"^1\.\s*", "", line).strip()
                # "/"로 보증금/월세 분리 (한글 단위 지원)
                price_match = re.search(
                    r'([\d억천백만원\s]+?)/([\d억천백만원\s]+)',
                    content1,
                )
                if price_match:
                    보증금 = PropertyParser._parse_korean_number(
                        price_match.group(1)
                    )
                    월세 = PropertyParser._parse_korean_number(
                        price_match.group(2)
                    )
                    if 보증금 is not None:
                        data["보증금"] = 보증금
                    if 월세 is not None:
                        data["월세"] = 월세
                # 부가세 판단 (부별, 부가세별도, 부가세o 등)
                if re.search(r'부\s*별|부가세\s*별도|부가세\s*[oO]', line):
                    data["부가세"] = "별도"
                elif re.search(
                    r'부\s*없|부\s*[xX]|부가세\s*[xX]|부가세\s*없', line
                ):
                    data["부가세"] = "없음"
                elif re.search(r'부가세|확인', line):
                    data["부가세"] = "확인필요"
                elif not skip_address:
                    # 신규 등록 시에만 기본값 설정
                    # 수정 모드에서는 기존 부가세 유지
                    data["부가세"] = "확인필요"

            # 2. 관리비
            elif line.startswith("2."):
                data["관리비"] = re.sub(r"^2\.\s*", "", line).strip()

            # 3. 권리금 (무권리, 권없, 권x 등)
            elif line.startswith("3."):
                rights_fee = re.sub(r"^3\.\s*", "", line).strip()
                # "권리금/권리/권" 접두사 제거
                # - "권리금"은 항상 제거
                # - "권리/권"은 뒤에 숫자가 올 때만 제거
                rights_text = re.sub(
                    r'^권리금\s*|^권(?:리)?\s*(?=\d)',
                    '', rights_fee
                ).strip()

                # 괄호 안 내용 추출 (메모용)
                paren_match = re.search(
                    r'[(\(](.+?)[)\)]', rights_text
                )
                paren_memo = (
                    paren_match.group(1).strip()
                    if paren_match
                    else ""
                )
                # 괄호 제거한 텍스트
                rights_clean = re.sub(
                    r'[(\(].+?[)\)]', '', rights_text
                ).strip()

                # 숫자가 먼저 있는지 확인
                num_match = re.match(r'(\d+)', rights_clean)

                if num_match:
                    # 숫자가 있으면 → 권리금 금액
                    data["권리금"] = int(num_match.group(1))
                    # 메모: 괄호 내용 우선, 없으면 숫자 뒤 텍스트
                    if paren_memo:
                        data["권리금 메모"] = paren_memo
                    else:
                        remaining = re.sub(
                            r'^\d+\s*', '', rights_clean
                        ).strip()
                        remaining = re.sub(
                            r'^만\s*원?\s*', '', remaining
                        ).strip()
                        if remaining:
                            data["권리금 메모"] = remaining
                elif (
                    re.search(
                        r'무권리|권\s*없|권\s*[xX]|권리금\s*[xX]',
                        rights_text,
                    )
                    or rights_text == "0"
                ):
                    # 무권리 계열
                    data["권리금"] = 0
                    # "무권리" 뒤 추가 텍스트 → 메모
                    remaining = re.sub(
                        r'무권리|권\s*없|권\s*[xX]|권리금\s*[xX]',
                        '', rights_text,
                    ).strip()
                    remaining = re.sub(
                        r'^[,\s]+', '', remaining
                    ).strip()
                    if paren_memo:
                        data["권리금 메모"] = paren_memo
                    elif remaining:
                        data["권리금 메모"] = remaining
                    else:
                        data["권리금 메모"] = "무권리"
                else:
                    data["권리금 메모"] = rights_text

            # 4. 건축물용도 / 면적 (복층/통상가 지원)
            elif line.startswith("4."):
                content4 = re.sub(r"^4\.\s*", "", line).strip()

                # 층별 구분 체크 (여러 패턴 지원)
                # 패턴 1: "1층 계약48.43㎡ 전용48.43㎡ 14평" (기존)
                # 패턴 2: "1층 48.43/48.43" (간소화, 평수 자동)
                # 패턴 3: "1층 40/50" (완전 간소화)
                
                # 모든 층 정보를 저장할 딕셔너리 (층 번호를 키로 사용)
                층별_정보 = {}

                # ── 면적 패턴 파싱 (다양한 입력 형식 통합 지원) ──
                # 지원 형식:
                #   1층 40/40          (기본)
                #   1층 계약40/40      (계약 접두사)
                #   1층 40/전용40      (전용 접두사)
                #   1층 계약40/전용40  (둘 다)
                #   1층 계약40,전용40  (콤마 구분)
                #   1층 계약40 전용40  (공백+전용 구분)
                #   1층 40.5/33.05     (소수점)
                #   1층 40㎡/33㎡      (단위 포함)
                면적_패턴 = re.findall(
                    r'(\d+)층\s+'
                    r'(?:계(?:약)?(?:면적)?\s*)?'   # 선택적 "계약" 접두사
                    r'(\d+\.?\d*)'                   # 계약면적 숫자
                    r'\s*(?:m2|㎡)?\s*'              # 선택적 단위
                    r'(?:[/,]\s*|\s+(?=전))'         # 구분자: / 또는 , 또는 "전용" 앞 공백
                    r'(?:전(?:용)?(?:면적)?\s*)?'    # 선택적 "전용" 접두사
                    r'(\d+\.?\d*)',                  # 전용면적 숫자
                    content4
                )

                for 층, 계약, 전용 in 면적_패턴:
                    계약_f = float(계약)
                    전용_f = float(전용)
                    평 = round(전용_f / 3.3, 1)
                    층별_정보[층] = {
                        '계약': 계약_f,
                        '전용': 전용_f,
                        '평': 평
                    }

                # 평수 명시 패턴 (예: "1층 계약48.43㎡ 전용48.43㎡ 14평")
                # 위 패턴에서 못 잡은 경우만 추가 처리
                상세_패턴 = re.findall(
                    r'(\d+)층[^/]*?'
                    r'(?:계(?:약)?(?:면적)?\s*)?(\d+\.?\d*)\s*(?:m2|㎡)?[^/]*?'
                    r'전(?:용)?(?:면적)?\s*(\d+\.?\d*)\s*(?:m2|㎡)?[^/]*?'
                    r'(?:약\s*)?(\d+\.?\d*)\s*평',
                    content4
                )

                for 층, 계약, 전용, 평 in 상세_패턴:
                    if 층 not in 층별_정보:  # 위 패턴과 중복 방지
                        층별_정보[층] = {
                            '계약': float(계약) if 계약 else 0,
                            '전용': float(전용) if 전용 else 0,
                            '평': float(평) if 평 else 0
                        }
                
                # 3단계: 총합 계산 및 층별면적상세 생성
                if 층별_정보:
                    총_계약 = sum(info['계약'] for info in 층별_정보.values())
                    총_전용 = sum(info['전용'] for info in 층별_정보.values())
                    
                    # 층 이모지 매핑
                    층_이모지 = {
                        '1': '1️⃣', '2': '2️⃣', '3': '3️⃣', '4': '4️⃣', '5': '5️⃣',
                        '6': '6️⃣', '7': '7️⃣', '8': '8️⃣', '9': '9️⃣', '10': '🔟'
                    }
                    
                    # 층 번호 순서대로 정렬
                    sorted_floors = sorted(층별_정보.keys(), key=int)
                    층별_평수_parts = []
                    
                    for 층 in sorted_floors:
                        평 = 층별_정보[층]['평']
                        # 소수점이 0이면 정수로 표시 (14.0 → 14)
                        if 평 == int(평):
                            평_str = str(int(평))
                        else:
                            평_str = str(평)
                        
                        # 이모지 추가 (예: 1️⃣14p)
                        이모지 = 층_이모지.get(층, f"{층}층")
                        층별_평수_parts.append(f"{이모지}{평_str}p")
                    
                    data["계약면적"] = 총_계약
                    data["전용면적"] = 총_전용
                    data["층별면적상세"] = " ".join(층별_평수_parts)
                else:
                    # 단일 매물 면적 파싱 (단위 없이도 인식, 다양한 구분자 지원)
                    # 지원 형식:
                    #   계약 144m2 / 전용 33m2   (기존, 단위 있음)
                    #   계약 144 / 전용 33        (단위 없음)
                    #   계약 144 전용 33          (공백 구분)
                    #   계약144/144              (슬래시)
                    #   계약면적144/144
                    #   144/전용144
                    #   144/전용면적144
                    #   144/144                  (키워드 없이)
                    #   계약144,전용144           (콤마)
                    found_area = False

                    # ── 1순위: 통합 패턴 (계약N[단위][구분자]전용N) ──
                    통합_match = re.search(
                        r'(?:계(?:약)?(?:면적)?\s*)?'
                        r'(\d+\.?\d*)\s*(?:m2|㎡)?\s*'
                        r'(?:[/,]\s*|\s+(?=전))'
                        r'(?:전(?:용)?(?:면적)?\s*)?'
                        r'(\d+\.?\d*)',
                        content4
                    )
                    if 통합_match:
                        data["계약면적"] = float(통합_match.group(1))
                        data["전용면적"] = float(통합_match.group(2))
                        found_area = True

                    if not found_area:
                        # ── 2순위: 계약/전용 키워드를 각각 따로 탐색 ──
                        # (중간에 "약10평" 같은 부가 텍스트가 있을 때 대비)
                        계약_match = re.search(
                            r"계(?:약)?(?:면적)?\s*(\d+\.?\d*)\s*(?:m2|㎡)?",
                            content4,
                        )
                        전용_match = re.search(
                            r"전(?:용)?(?:면적)?\s*(\d+\.?\d*)\s*(?:m2|㎡)?",
                            content4,
                        )
                        if 계약_match:
                            data["계약면적"] = float(계약_match.group(1))
                        if 전용_match:
                            data["전용면적"] = float(전용_match.group(1))

                # ── 건축물용도 파싱 (층별 다용도 지원) ──
                floor_use_pairs = PropertyParser._parse_floor_uses(content4)

                if len(floor_use_pairs) > 1:
                    # 복층/통상가: 층별 용도가 서로 다름
                    # 중복 제거 (순서 유지)
                    seen_uses: List[str] = []
                    seen_set: set = set()
                    for _, use in floor_use_pairs:
                        if use not in seen_set:
                            seen_uses.append(use)
                            seen_set.add(use)
                    data["건축물용도"] = seen_uses  # 리스트 → multi_select

                    # 층별용도 문자열 생성: "1층 제1종 / 2,3층 제2종"
                    abbr_parts = []
                    for fl, use in floor_use_pairs:
                        abbr = PropertyParser._abbreviate_building_use(use)
                        abbr_parts.append(f"{fl}층 {abbr}")
                    data["층별용도"] = " / ".join(abbr_parts)

                elif len(floor_use_pairs) == 1:
                    # 단일 층 용도 명시
                    data["건축물용도"] = [floor_use_pairs[0][1]]

                else:
                    # 층 구분 없음 → 기존 방식: 앞부분에서 용도 추출
                    용도_text = re.split(
                        r'\s*/\s*계약(?:면적)?|\s+계약(?:면적)?'
                        r'|\s*/\s*전용(?:면적)?|\s+전용(?:면적)?'
                        r'|\s*\d+층',
                        content4,
                    )[0].strip().rstrip(' /')
                    if 용도_text:
                        data["건축물용도"] = [
                            PropertyParser._normalize_building_use(용도_text)
                        ]

            # 5. 주차 / 화장실
            elif line.startswith("5."):
                content5 = re.sub(r"^5\.\s*", "", line).strip()
                parts5 = [p.strip() for p in content5.split("/")]

                parking_parts = []
                bathroom_parts = []
                for part in parts5:
                    if "화장실" in part:
                        bathroom_parts.append(part)
                    else:
                        parking_parts.append(part)

                parking_text = " ".join(parking_parts).strip()

                # 주차 판단
                if parking_text and "주차" in parking_text:
                    # "주차 불가", "주차X", "주차 안 됨" → 불가능
                    if re.search(
                        r'주차\s*[xX]|주차\s*불가|주차\s*안\s*됨',
                        parking_text,
                    ):
                        data["주차"] = "불가능"
                    else:
                        # 그 외 ("주차 가능", "주차 o", "주차(매장앞1대)" 등) → 가능
                        data["주차"] = "가능"
                        
                        # 주차 메모 추출
                        pmemo = re.sub(
                            r'^주차\s*[는은]?\s*', '', parking_text
                        ).strip()
                        # "o", "O", "ㅇ", "가능" 제거
                        pmemo = re.sub(r'^[oOㅇ가능]\s*', '', pmemo).strip()
                        pmemo = re.sub(r'^가능\s*', '', pmemo).strip()
                        pmemo = re.sub(r'^장\s*사용', '주차장', pmemo)
                        
                        # 괄호 내용은 유지하되 괄호만 제거
                        pmemo = pmemo.replace('(', '').replace(')', '')
                        
                        pmemo = re.sub(
                            r'하긴한데|애매|선착순', '', pmemo
                        ).strip()
                        
                        # 한글과 숫자 사이 공백 추가 (기계식60대 → 기계식 60대)
                        pmemo = re.sub(
                            r'([가-힣])(\d)', r'\1 \2', pmemo
                        )
                        pmemo = re.sub(r'\s+', ' ', pmemo).strip()
                        
                        if pmemo:
                            data["주차 메모"] = pmemo

                # 화장실 파싱
                for part in bathroom_parts:
                    화장실_match = re.search(r"화장실\s*(\d+)", part)
                    if 화장실_match:
                        data["화장실 수"] = f"{화장실_match.group(1)}개"
                    if "내부" in part:
                        data["화장실 위치"] = "내부"
                    elif "외부" in part:
                        data["화장실 위치"] = "외부"

            # 6. 방향
            elif line.startswith("6."):
                방향_match = re.search(
                    r"(남향|북향|동향|서향|남동향|남서향|북동향|북서향)", line
                )
                if 방향_match:
                    data["방향"] = 방향_match.group(1)

            # 7. 위반건축물 (대장 기반 판단)
            elif line.startswith("7."):
                # 위반건축물O (위반 있음)
                if re.search(
                    r'위반\s*[oOㅇ]|대장\s*(위반|불법|위법)', line
                ):
                    data["위반건축물"] = "위반건축물O"
                # 위반건축물X (정상)
                elif re.search(
                    r'위반\s*[xXㅌ]|대장\s*[oOㅇ]'
                    r'|대장\s*이상\s*[무없]|대장\s*정상',
                    line,
                ):
                    data["위반건축물"] = "위반건축물X"

            # 8. 연락처 (다중: "/" 구분 또는 줄바꿈)
            elif line.startswith("8."):
                in_contacts = True
                contact_idx = 0
                content = re.sub(r"^8\.\s*", "", line).strip()
                contacts = [
                    c.strip() for c in content.split("/")
                ]
                for contact in contacts:
                    PropertyParser._store_contact(
                        data, contact, contact_idx
                    )
                    contact_idx += 1

            # 8번 이후 줄바꿈 추가 연락처
            elif in_contacts and not is_numbered:
                phone_check = re.search(
                    r'\d{2,3}[-\s]*\d{3,4}[-\s]*\d{4}', line
                )
                if phone_check and contact_idx < 3:
                    PropertyParser._store_contact(
                        data, line, contact_idx
                    )
                    contact_idx += 1
                else:
                    # 전화번호 없는 줄 → 특이사항으로 전환
                    in_contacts = False
                    in_special_section = True
                    special_notes.append(line)

            # 번호 형식도 아니고 연락처도 아닌 줄 → 특이사항
            elif not is_numbered and data:
                in_special_section = True
                special_notes.append(line)

        if special_notes:
            data["특이사항"] = "\n".join(special_notes)

        return data

    @staticmethod
    def _store_contact(
        data: Dict, contact: str, idx: int
    ):
        """연락처 정보를 data 딕셔너리에 저장

        Args:
            data: 파싱 결과 딕셔너리
            contact: 연락처 텍스트 (예: "양도인 010 5771 6577")
            idx: 연락처 인덱스 (0=대표, 1=추가1, 2=추가2)
        """
        if idx > 2:
            return
        phone_match = re.search(
            r"(\d{2,3}[-\s]*\d{3,4}[-\s]*\d{4})", contact
        )
        memo_match = re.search(
            r"([가-힣]+(?:\s+[가-힣]+)*)", contact
        )

        phone = (
            phone_match.group(1) if phone_match else ""
        )
        memo = memo_match.group(1) if memo_match else ""

        if idx == 0:
            if phone:
                data["대표 연락처"] = phone
            if memo:
                data["연락처 메모"] = memo
        elif idx == 1:
            if phone:
                data["추가 연락처1"] = phone
            if memo:
                data["연락처 추가메모1"] = memo
        elif idx == 2:
            if phone:
                data["추가 연락처2"] = phone
            if memo:
                data["연락처 추가메모2"] = memo

    @staticmethod
    def _parse_korean_number(text: str) -> Optional[int]:
        """한글 숫자 표현을 만원 단위 정수로 변환

        예: '1억6천' → 16000, '1300만원' → 1300, '2000' → 2000
            '5천' → 5000, '1억' → 10000
        """
        text = text.strip()
        if not text:
            return None

        total = 0
        has_unit = False

        # 억 단위 (1억 = 10000만원)
        억_match = re.search(r'(\d+)\s*억', text)
        if 억_match:
            total += int(억_match.group(1)) * 10000
            has_unit = True

        # 천 단위 (1천 = 1000만원)
        천_match = re.search(r'(\d+)\s*천', text)
        if 천_match:
            total += int(천_match.group(1)) * 1000
            has_unit = True

        # 백 단위 (1백 = 100만원)
        백_match = re.search(r'(\d+)\s*백', text)
        if 백_match:
            total += int(백_match.group(1)) * 100
            has_unit = True

        if has_unit:
            # 단위 제거 후 남은 숫자가 있으면 더하기
            # 예: "1억5000" → 1*10000 + 5000 = 15000
            remaining = re.sub(r'\d+\s*[억천백]', '', text)
            remaining = re.sub(r'[만원\s]', '', remaining).strip()
            extra = re.search(r'(\d+)', remaining)
            if extra:
                total += int(extra.group(1))
            return total

        # 단순 숫자만 있는 경우 (만원/원 제거)
        clean = re.sub(r'[만원\s]', '', text)
        num_match = re.search(r'(\d+)', clean)
        if num_match:
            return int(num_match.group(1))

        return None

    @staticmethod
    def _normalize_building_use(text: str) -> str:
        """건축물용도 약어를 정식 명칭으로 정규화
        
        다양한 표기법 지원:
        1종, 제1종, 1종근생, 1종근린, 근생1종, 제1종근린생활시설 등
        """
        text = text.strip()
        # 1종 근린생활시설 계열 (다양한 약어 포함)
        if re.search(
            r'(?:제\s*)?1\s*종'
            r'|1\s*종\s*근\s*(?:린\s*)?(?:생)?'
            r'|근\s*생\s*1\s*종'
            r'|근\s*린\s*1\s*종',
            text
        ):
            return "제1종근린생활시설"
        # 2종 근린생활시설 계열
        if re.search(
            r'(?:제\s*)?2\s*종'
            r'|2\s*종\s*근\s*(?:린\s*)?(?:생)?'
            r'|근\s*생\s*2\s*종'
            r'|근\s*린\s*2\s*종',
            text
        ):
            return "제2종근린생활시설"
        if re.search(r'판\s*매\s*시\s*설', text):
            return "판매시설"
        if re.search(r'위\s*락\s*시\s*설', text):
            return "위락시설"
        if re.search(r'숙\s*박\s*시\s*설', text):
            return "숙박시설"
        if re.search(r'의\s*료\s*시\s*설', text):
            return "의료시설"
        if re.search(r'교\s*육\s*(?:연\s*구\s*)?시\s*설', text):
            return "교육연구시설"
        if re.search(r'업\s*무\s*시\s*설', text):
            return "업무시설"
        if re.search(r'수\s*련\s*시\s*설', text):
            return "수련시설"
        if re.search(r'공\s*장', text):
            return "공장"
        if re.search(r'창\s*고', text):
            return "창고시설"
        return text

    @staticmethod
    def _abbreviate_building_use(full_use: str) -> str:
        """노션 층별용도 표시용 약칭 변환 (짧게)"""
        return {
            "제1종근린생활시설": "제1종",
            "제2종근린생활시설": "제2종",
            "판매시설": "판매",
            "위락시설": "위락",
            "숙박시설": "숙박",
            "의료시설": "의료",
            "교육연구시설": "교육",
            "업무시설": "업무",
            "수련시설": "수련",
            "공장": "공장",
            "창고시설": "창고",
        }.get(full_use, full_use)

    @staticmethod
    def _parse_floor_uses(text: str) -> List[Tuple[str, str]]:
        """층별 건축물 용도 파싱

        "1층 1종근생 2,3층 2종근생" →
            [("1", "제1종근린생활시설"), ("2,3", "제2종근린생활시설")]

        Args:
            text: 4번 섹션 원본 텍스트

        Returns:
            [(floor_key, normalized_use), ...] 순서 유지
        """
        # 면적 패턴(숫자/숫자) 제거 → 용도만 남김
        cleaned = re.sub(r'\d+\.?\d*\s*/\s*\d+\.?\d*', '', text)
        # 괄호 안 평수 정보 제거
        cleaned = re.sub(r'\([^)]*\)', '', cleaned)
        # 면적 단위 텍스트 제거 (계약 144m2, 전용 33m2 등)
        cleaned = re.sub(
            r'(?:계(?:약)?|전(?:용)?)\s*\d+\.?\d*\s*(?:m2|㎡)',
            '', cleaned
        )
        cleaned = cleaned.strip()

        # 층 마커 위치 탐색 (예: 1층, 2층, 2,3층, 1~3층)
        floor_markers = list(
            re.finditer(r'(\d+(?:[,~\-]\d+)*)\s*층', cleaned)
        )
        if not floor_markers:
            return []

        results = []
        for i, marker in enumerate(floor_markers):
            floor_key = marker.group(1)
            # 용도 텍스트: 이 층 마커 끝 ~ 다음 층 마커 시작
            start = marker.end()
            end = (
                floor_markers[i + 1].start()
                if i + 1 < len(floor_markers)
                else len(cleaned)
            )
            use_text = cleaned[start:end].strip()
            # 불필요한 앞뒤 문자 제거
            use_text = re.sub(r'^[\s,]+|[\s,]+$', '', use_text)
            if use_text:
                normalized = PropertyParser._normalize_building_use(
                    use_text
                )
                results.append((floor_key, normalized))

        return results

    @staticmethod
    def _merge_section4_lines(text: str) -> str:
        """4번 섹션의 연속 줄(다층 면적/용도)을 한 줄로 합치기

        예:
          4. 1층 1종근생 2,3층 2종근생
             1층 40/40 2층 50/50 3층 30/30
          →
          4. 1층 1종근생 2,3층 2종근생 1층 40/40 2층 50/50 3층 30/30
        """
        lines = text.split('\n')
        result = []
        in_section4 = False

        for line in lines:
            stripped = line.strip()
            is_numbered = bool(re.match(r'^\d+\.', stripped))

            if stripped.startswith('4.'):
                in_section4 = True
                result.append(stripped)
            elif in_section4 and not is_numbered and stripped:
                # 층/면적 패턴이 있으면 앞 줄에 이어 붙임
                looks_like_continuation = bool(
                    re.search(r'\d+층|\d+\.?\d*/\d+', stripped)
                )
                if looks_like_continuation and result:
                    result[-1] = result[-1] + ' ' + stripped
                else:
                    in_section4 = False
                    result.append(line)
            else:
                if is_numbered:
                    in_section4 = False
                result.append(line)

        return '\n'.join(result)


class NotionUploader:
    """노션 업로드 클래스"""

    def __init__(self, notion_token: str, database_id: str):
        self.client = Client(auth=notion_token)
        self.database_id = database_id

    def ensure_sync_properties(self):
        """동기화에 필요한 Notion 속성 생성 (없으면 추가)"""
        try:
            self.client.databases.update(
                database_id=self.database_id,
                properties={
                    "telegram_chat_id": {"number": {}},
                    "telegram_msg_id": {"number": {}},
                    # 층별 용도 상세 필드
                    "층별용도": {"rich_text": {}},
                    # 거래 완료 관련
                    "거래완료 시점": {"rich_text": {}},
                    # 계약 담당자 (select)
                    "계약담당자": {"select": {}},
                },
            )
            logger.info("동기화용 Notion 속성 확인 완료")
        except Exception as e:
            logger.warning(
                f"동기화 속성 생성/확인 실패 (무시): {e}"
            )

    def _build_notion_properties(
        self, property_data: Dict, is_update: bool = False
    ) -> Dict:
        """property_data로부터 노션 properties 딕셔너리 생성

        Args:
            property_data: 파싱된 매물 정보
            is_update: True이면 수정 모드 (등록 날짜, 거래 상태 유지)
        """
        properties = {}

        # ── 주소 및 상호 (title) ──
        if "주소" in property_data:
            properties["주소 및 상호"] = {
                "title": [
                    {"text": {"content": property_data["주소"]}}
                ]
            }
        elif not is_update:
            properties["주소 및 상호"] = {
                "title": [{"text": {"content": "매물"}}]
            }

        # ── 층수 (multi_select) ──
        주소 = property_data.get("주소", "")
        층_list = []
        
        # 1. 범위 형식 우선: "1~3층", "1-3층"
        범위_match = re.search(r'(\d+)[~\-](\d+)층', 주소)
        if 범위_match:
            start = int(범위_match.group(1))
            end = int(범위_match.group(2))
            층_list = [f"{i}층" for i in range(start, end + 1)]
        else:
            # 2. 콤마 구분 형식: "1,2,3층"
            콤마_match = re.search(r'(\d+(?:,\d+)+)층', 주소)
            if 콤마_match:
                층_numbers = 콤마_match.group(1).split(',')
                층_list = [f"{층.strip()}층" for 층 in 층_numbers]
            else:
                # 3. 연속 층 형식: "2층3층" 또는 "1층 2층 3층" (띄어쓰기 0~2개)
                연속_matches = re.findall(r'(\d+)층', 주소)
                if len(연속_matches) > 1:
                    # 여러 층이 감지되면 모두 추가
                    층_list = [f"{층}층" for 층 in 연속_matches]
                elif len(연속_matches) == 1:
                    # 4. 단일 층 형식: "1층"
                    층_list = [f"{연속_matches[0]}층"]
        
        if 층_list:
            properties["층수"] = {
                "multi_select": [{"name": 층} for 층 in 층_list]
            }

        # ── 💰보증금 (number) ──
        if "보증금" in property_data:
            properties["💰보증금"] = {
                "number": property_data["보증금"]
            }

        # ── 💰월세 (number) ──
        if "월세" in property_data:
            properties["💰월세"] = {"number": property_data["월세"]}

        # ── 🧾부가세 여부 (select) ──
        if "부가세" in property_data:
            properties["🧾부가세 여부"] = {
                "select": {"name": property_data["부가세"]}
            }

        # ── ⚡관리비(텍스트) (rich_text) ──
        if "관리비" in property_data:
            properties["⚡관리비(텍스트)"] = {
                "rich_text": [
                    {"text": {"content": property_data["관리비"]}}
                ]
            }

        # ── 💎권리금 (number) ──
        if "권리금" in property_data:
            if isinstance(property_data["권리금"], int):
                properties["💎권리금"] = {
                    "number": property_data["권리금"]
                }

        # ── 권리금 메모 (rich_text) ──
        if "권리금 메모" in property_data:
            properties["권리금 메모"] = {
                "rich_text": [
                    {"text": {"content": property_data["권리금 메모"]}}
                ]
            }

        # ── 🏢건축물용도 (multi_select) ──
        # property_data["건축물용도"]는 리스트 (예: ["제1종근린생활시설", "제2종근린생활시설"])
        if "건축물용도" in property_data:
            용도_value = property_data["건축물용도"]
            if isinstance(용도_value, list):
                용도_list = 용도_value
            else:
                # 이전 버전 호환: 문자열로 저장된 경우
                용도_list = [용도_value]
            properties["🏢건축물용도"] = {
                "multi_select": [
                    {"name": use[:100]}
                    for use in 용도_list
                    if use
                ]
            }

        # ── 층별용도 (rich_text) - 복층/통상가 전용 ──
        if "층별용도" in property_data:
            properties["층별용도"] = {
                "rich_text": [
                    {
                        "text": {
                            "content": property_data["층별용도"][:2000]
                        }
                    }
                ]
            }

        # ── 🏢 매물 유형 (select) ──
        if "매물_유형" in property_data:
            properties["🏢 매물 유형"] = {
                "select": {"name": property_data["매물_유형"]}
            }
        
        # ── 📍소재지(구) (select) ──
        if "소재지_구" in property_data:
            properties["📍소재지(구)"] = {
                "select": {"name": property_data["소재지_구"]}
            }
        
        # ── 임대 구분 (select) ──
        if "임대_구분" in property_data:
            properties["임대 구분"] = {
                "select": {"name": property_data["임대_구분"]}
            }

        # ── 📐계약면적(m²) (number) ──
        if "계약면적" in property_data:
            properties["📐계약면적(m²)"] = {
                "number": property_data["계약면적"]
            }

        # ── 📐전용면적(m²) (number) ──
        if "전용면적" in property_data:
            properties["📐전용면적(m²)"] = {
                "number": property_data["전용면적"]
            }

        # ── 📐 층별면적상세 (rich_text) ──
        if "층별면적상세" in property_data:
            properties["📐 층별면적상세"] = {
                "rich_text": [
                    {"text": {"content": property_data["층별면적상세"]}}
                ]
            }

        # ── 🅿️주차 (select) ──
        if "주차" in property_data:
            properties["🅿️주차"] = {
                "select": {"name": property_data["주차"]}
            }

        # ── 주차 메모 (rich_text) ──
        if "주차 메모" in property_data:
            properties["주차 메모"] = {
                "rich_text": [
                    {"text": {"content": property_data["주차 메모"]}}
                ]
            }

        # ── 📍방향 (select) ──
        if "방향" in property_data:
            properties["📍방향"] = {
                "select": {"name": property_data["방향"]}
            }

        # ── 🚻화장실 위치 (select) ──
        if "화장실 위치" in property_data:
            properties["🚻화장실 위치"] = {
                "select": {"name": property_data["화장실 위치"]}
            }

        # ── 🚻화장실 수 (select) ──
        if "화장실 수" in property_data:
            properties["🚻화장실 수"] = {
                "select": {"name": property_data["화장실 수"]}
            }

        # ── 🚨위반건축물 (select) ──
        if "위반건축물" in property_data:
            properties["🚨위반건축물"] = {
                "select": {"name": property_data["위반건축물"]}
            }

        # ── 🙋🏻‍♂️매물접수 (multi_select) ──
        if "매물접수" in property_data:
            properties["🙋🏻‍♂️매물접수"] = {
                "multi_select": [{"name": property_data["매물접수"]}]
            }

        # ── 📅등록 날짜 (date) - 신규 등록 시에만 ──
        if not is_update:
            properties["📅등록 날짜"] = {
                "date": {
                    "start": datetime.now().date().isoformat()
                }
            }

        # ── 📢 특이사항 (rich_text) ──
        if "특이사항" in property_data:
            properties["📢 특이사항"] = {
                "rich_text": [
                    {
                        "text": {
                            "content": property_data["특이사항"][:2000]
                        }
                    }
                ]
            }

        # ── 연락처 메모 (rich_text) ──
        if "연락처 메모" in property_data:
            properties["연락처 메모"] = {
                "rich_text": [
                    {
                        "text": {
                            "content": property_data["연락처 메모"]
                        }
                    }
                ]
            }

        # ── 📞 대표 연락처 (phone_number) ──
        if "대표 연락처" in property_data:
            properties["📞 대표 연락처"] = {
                "phone_number": property_data["대표 연락처"]
            }

        # ── 연락처 추가메모1 (rich_text) ──
        if "연락처 추가메모1" in property_data:
            properties["연락처 추가메모1"] = {
                "rich_text": [
                    {
                        "text": {
                            "content": property_data["연락처 추가메모1"]
                        }
                    }
                ]
            }

        # ── 추가 연락처1 (phone_number) ──
        if "추가 연락처1" in property_data:
            properties["추가 연락처1"] = {
                "phone_number": property_data["추가 연락처1"]
            }

        # ── 연락처 추가메모2 (rich_text) ──
        if "연락처 추가메모2" in property_data:
            properties["연락처 추가메모2"] = {
                "rich_text": [
                    {
                        "text": {
                            "content": property_data["연락처 추가메모2"]
                        }
                    }
                ]
            }

        # ── 추가 연락처2 (phone_number) ──
        if "추가 연락처2" in property_data:
            properties["추가 연락처2"] = {
                "phone_number": property_data["추가 연락처2"]
            }

        # ── 거래 상태 (select) ──
        if "거래_상태" in property_data:
            properties["거래 상태"] = {
                "select": {"name": property_data["거래_상태"]}
            }
        elif not is_update:
            # 신규 등록 시에만 기본값 설정
            properties["거래 상태"] = {
                "select": {"name": "거래 가능"}
            }
        
        # ── 거래완료 시점 (rich_text) ──
        if "거래완료_시점" in property_data:
            properties["거래완료 시점"] = {
                "rich_text": [
                    {"text": {"content": property_data["거래완료_시점"]}}
                ]
            }

        # ── 계약담당자 (select) ──
        if "계약담당자" in property_data:
            properties["계약담당자"] = {
                "select": {"name": property_data["계약담당자"]}
            }

        # ── 텔레그램 동기화 정보 (number) ──
        if "telegram_chat_id" in property_data:
            properties["telegram_chat_id"] = {
                "number": property_data["telegram_chat_id"]
            }
        if "telegram_msg_id" in property_data:
            properties["telegram_msg_id"] = {
                "number": property_data["telegram_msg_id"]
            }

        return properties

    @staticmethod
    def _build_photo_blocks(photo_urls: List[str]) -> List[Dict]:
        """사진 URL 목록을 노션 블록 목록으로 변환 (2열 레이아웃)"""
        blocks = []
        for i in range(0, len(photo_urls), 2):
            pair = photo_urls[i: i + 2]
            if len(pair) == 2:
                blocks.append(
                    {
                        "object": "block",
                        "type": "column_list",
                        "column_list": {
                            "children": [
                                {
                                    "object": "block",
                                    "type": "column",
                                    "column": {
                                        "children": [
                                            {
                                                "object": "block",
                                                "type": "image",
                                                "image": {
                                                    "type": "external",
                                                    "external": {
                                                        "url": pair[0]
                                                    },
                                                },
                                            }
                                        ]
                                    },
                                },
                                {
                                    "object": "block",
                                    "type": "column",
                                    "column": {
                                        "children": [
                                            {
                                                "object": "block",
                                                "type": "image",
                                                "image": {
                                                    "type": "external",
                                                    "external": {
                                                        "url": pair[1]
                                                    },
                                                },
                                            }
                                        ]
                                    },
                                },
                            ]
                        },
                    }
                )
            else:
                # 홀수 마지막 1장은 전체 너비
                blocks.append(
                    {
                        "object": "block",
                        "type": "image",
                        "image": {
                            "type": "external",
                            "external": {"url": pair[0]},
                        },
                    }
                )
        return blocks

    def upload_property(
        self,
        property_data: Dict,
        photo_urls: Optional[List[str]] = None,
        floor_photos: Optional[List[Dict]] = None,
    ) -> Tuple[str, str]:
        """
        노션 데이터베이스에 매물 등록 (층별 사진 헤딩 지원)

        Args:
            property_data: 파싱된 매물 정보
            photo_urls: flat 사진 URL 목록 (floor_photos 없을 때 사용)
            floor_photos: 층별 사진 그룹
                [{"label": "1층", "photos": [url, ...]}, ...]
                라벨이 있으면 노션에 헤딩을 표시

        Returns:
            (page_url, page_id) 튜플
        """
        properties = self._build_notion_properties(property_data)

        # ──────────────────────────────────────────────
        # 페이지 내용 (본문 블록) - 층별 사진 헤딩 지원
        # ──────────────────────────────────────────────
        children = []

        if floor_photos and any(
            g.get("photos") for g in floor_photos
        ):
            # 층별 구분 사진 (헤딩 + 사진 그룹)
            for group in floor_photos:
                label = group.get("label")
                photos = group.get("photos", [])
                if not photos:
                    continue

                # 층 헤딩 추가 (라벨이 있을 때만)
                if label:
                    children.append(
                        {
                            "object": "block",
                            "type": "heading_2",
                            "heading_2": {
                                "rich_text": [
                                    {
                                        "text": {
                                            "content": f"📷 {label}"
                                        }
                                    }
                                ]
                            },
                        }
                    )

                # 해당 층 사진 추가 (2열 레이아웃)
                children.extend(
                    self._build_photo_blocks(photos)
                )

        elif photo_urls:
            # 층 구분 없는 flat 사진 목록
            children.extend(
                self._build_photo_blocks(photo_urls)
            )

        # 특이사항 블록
        if "특이사항" in property_data:
            children.append(
                {
                    "object": "block",
                    "type": "heading_2",
                    "heading_2": {
                        "rich_text": [
                            {"text": {"content": "특이사항"}}
                        ]
                    },
                }
            )
            for paragraph in property_data["특이사항"].split("\n"):
                if paragraph.strip():
                    children.append(
                        {
                            "object": "block",
                            "type": "paragraph",
                            "paragraph": {
                                "rich_text": [
                                    {
                                        "text": {
                                            "content": paragraph
                                        }
                                    }
                                ]
                            },
                        }
                    )

        # 원본 메시지
        if "원본 메시지" in property_data:
            children.append(
                {
                    "object": "block",
                    "type": "divider",
                    "divider": {},
                }
            )
            children.append(
                {
                    "object": "block",
                    "type": "heading_3",
                    "heading_3": {
                        "rich_text": [
                            {"text": {"content": "원본 메시지"}}
                        ]
                    },
                }
            )
            children.append(
                {
                    "object": "block",
                    "type": "paragraph",
                    "paragraph": {
                        "rich_text": [
                            {
                                "text": {
                                    "content": property_data[
                                        "원본 메시지"
                                    ][:2000]
                                }
                            }
                        ]
                    },
                }
            )

        # 노션 페이지 생성
        try:
            create_kwargs = {
                "parent": {"database_id": self.database_id},
                "properties": properties,
            }
            if children:
                create_kwargs["children"] = children

            response = self.client.pages.create(**create_kwargs)
            page_id = response["id"]
            # ID만으로 URL 생성 (제목 포함 방지 → 검색 깔끔)
            clean_url = (
                f"https://www.notion.so/"
                f"{page_id.replace('-', '')}"
            )
            return clean_url, page_id
        except Exception as e:
            logger.error(f"노션 업로드 실패: {e}")
            raise Exception(f"노션 업로드 실패: {str(e)}")

    def update_property(
        self, page_id: str, property_data: Dict
    ) -> str:
        """
        기존 노션 페이지의 매물 정보 수정

        Args:
            page_id: 수정할 노션 페이지 ID
            property_data: 수정할 매물 정보

        Returns:
            수정된 페이지 URL
        """
        properties = self._build_notion_properties(
            property_data, is_update=True
        )

        try:
            self.client.pages.update(
                page_id=page_id, properties=properties
            )
            # ID만으로 URL 생성 (제목 포함 방지)
            return (
                f"https://www.notion.so/"
                f"{page_id.replace('-', '')}"
            )
        except Exception as e:
            logger.error(f"노션 업데이트 실패: {e}")
            raise Exception(f"노션 업데이트 실패: {str(e)}")

    def archive_property(self, page_id: str) -> bool:
        """
        노션 페이지를 아카이브(삭제) 처리

        Args:
            page_id: 아카이브할 노션 페이지 ID

        Returns:
            성공 여부
        """
        try:
            self.client.pages.update(
                page_id=page_id, archived=True
            )
            logger.info(f"노션 페이지 아카이브 완료: {page_id}")
            return True
        except Exception as e:
            logger.error(f"노션 아카이브 실패: {e}")
            raise Exception(f"노션 아카이브 실패: {str(e)}")

    def update_deal_status(
        self, page_id: str, agent_name: str = None
    ) -> bool:
        """거래 상태를 '거래 완료'로 업데이트하고 계약담당자 기록

        Args:
            page_id: 업데이트할 노션 페이지 ID
            agent_name: 계약 담당자 이름 (없으면 None)

        Returns:
            성공 여부
        """
        try:
            now = datetime.now()
            properties = {
                "거래 상태": {
                    "select": {"name": "거래 완료"}
                },
                "거래완료 시점": {
                    "rich_text": [
                        {"text": {"content": now.strftime("%Y-%m-%d %H:%M")}}
                    ]
                },
            }
            if agent_name:
                properties["계약담당자"] = {
                    "select": {"name": agent_name}
                }
            self.client.pages.update(
                page_id=page_id,
                properties=properties,
            )
            logger.info(
                f"거래완료 업데이트 성공: page={page_id}, "
                f"담당자={agent_name}"
            )
            return True
        except Exception as e:
            logger.error(f"거래완료 업데이트 실패: {e}")
            return False

    def append_blocks_to_page(
        self, page_id: str, blocks: List[Dict]
    ) -> bool:
        """기존 노션 페이지 하단에 블록 추가 (추가사진 등)"""
        try:
            self.client.blocks.children.append(
                block_id=page_id,
                children=blocks,
            )
            return True
        except Exception as e:
            logger.error(f"노션 블록 추가 실패: {e}")
            return False

    def find_page_by_msg_id(self, msg_id: int) -> Optional[str]:
        """telegram_msg_id로 노션 페이지 ID 조회 (봇 재시작 후 복구용)"""
        try:
            response = self.client.databases.query(
                database_id=self.database_id,
                filter={
                    "property": "telegram_msg_id",
                    "number": {"equals": msg_id},
                },
                page_size=1,
            )
            for page in response.get("results", []):
                if not page.get("archived", False):
                    return page["id"]
            return None
        except Exception as e:
            logger.error(f"msg_id 검색 실패: {e}")
            return None

    def find_pages_by_address(
        self, address: str, exclude_page_id: str = None
    ) -> List[Dict]:
        """주소로 노션 페이지 검색 (동일 주소 중복 감지용)

        Args:
            address: 검색할 주소 문자열
            exclude_page_id: 결과에서 제외할 페이지 ID (새로 만든 페이지)

        Returns:
            [{"page_id": str, "title": str, "url": str}, ...]
        """
        try:
            # 괄호 부분 제거 후 핵심 주소만 사용 (너무 짧으면 오탐 방지)
            clean_addr = address.split("(")[0].strip()
            if len(clean_addr) < 5:
                return []

            results = []
            has_more = True
            start_cursor = None

            while has_more:
                query_params: Dict = {
                    "database_id": self.database_id,
                    "filter": {
                        "property": "주소 및 상호",
                        "title": {"contains": clean_addr},
                    },
                    "page_size": 100,
                }
                if start_cursor:
                    query_params["start_cursor"] = start_cursor

                response = self.client.databases.query(**query_params)

                for page in response.get("results", []):
                    if page.get("archived", False):
                        continue
                    pid = page["id"]
                    # 방금 생성한 페이지 제외
                    if exclude_page_id and (
                        pid.replace("-", "")
                        == exclude_page_id.replace("-", "")
                    ):
                        continue

                    props = page.get("properties", {})
                    title_list = props.get(
                        "주소 및 상호", {}
                    ).get("title", [])
                    title = (
                        title_list[0]
                        .get("text", {})
                        .get("content", "")
                        if title_list
                        else ""
                    )
                    results.append(
                        {
                            "page_id": pid,
                            "title": title,
                            "url": (
                                "https://www.notion.so/"
                                f"{pid.replace('-', '')}"
                            ),
                        }
                    )

                has_more = response.get("has_more", False)
                start_cursor = response.get("next_cursor")

            return results
        except Exception as e:
            logger.error(f"주소 검색 실패: {e}")
            return []

    def get_page_properties(self, page_id: str) -> Dict:
        """노션 페이지의 현재 속성값을 파싱하여 반환"""
        try:
            page = self.client.pages.retrieve(page_id=page_id)
            props = page.get("properties", {})
            result = {}

            # 주소 (title)
            if "주소 및 상호" in props:
                title_arr = props["주소 및 상호"].get(
                    "title", []
                )
                if title_arr:
                    result["주소"] = (
                        title_arr[0]
                        .get("text", {})
                        .get("content", "")
                    )

            # 층수 (multi_select)
            if "층수" in props:
                ms = props["층수"].get("multi_select", [])
                if ms:
                    result["층수"] = ms[0].get("name", "")

            # 숫자 속성
            for key, notion_key in [
                ("보증금", "💰보증금"),
                ("월세", "💰월세"),
                ("권리금", "💎권리금"),
                ("계약면적", "📐계약면적(m²)"),
                ("전용면적", "📐전용면적(m²)"),
            ]:
                if notion_key in props:
                    val = props[notion_key].get("number")
                    if val is not None:
                        # float → int 변환 (2000.0 → 2000)
                        result[key] = (
                            int(val) if val == int(val) else val
                        )

            # 선택 속성
            for key, notion_key in [
                ("부가세", "🧾부가세 여부"),
                ("주차", "🅿️주차"),
                ("방향", "📍방향"),
                ("화장실 위치", "🚻화장실 위치"),
                ("화장실 수", "🚻화장실 수"),
                ("위반건축물", "🚨위반건축물"),
                ("매물_유형", "🏢 매물 유형"),
                ("소재지_구", "📍소재지(구)"),
                ("임대_구분", "임대 구분"),
                ("거래_상태", "거래 상태"),
            ]:
                if notion_key in props:
                    sel = props[notion_key].get("select")
                    if sel:
                        result[key] = sel.get("name", "")

            # 건축물용도 (multi_select) - 리스트로 반환
            if "🏢건축물용도" in props:
                ms = props["🏢건축물용도"].get("multi_select", [])
                if ms:
                    result["건축물용도"] = [
                        item.get("name", "")
                        for item in ms
                        if item.get("name")
                    ]

            # 텍스트 속성
            for key, notion_key in [
                ("관리비", "⚡관리비(텍스트)"),
                ("특이사항", "📢 특이사항"),
            ]:
                if notion_key in props:
                    rt = props[notion_key].get(
                        "rich_text", []
                    )
                    if rt:
                        result[key] = (
                            rt[0]
                            .get("text", {})
                            .get("content", "")
                        )

            # 전화번호 속성
            if "📞 대표 연락처" in props:
                val = props["📞 대표 연락처"].get(
                    "phone_number"
                )
                if val:
                    result["대표 연락처"] = val

            return result
        except Exception as e:
            logger.warning(f"페이지 속성 조회 실패: {e}")
            return {}

    def get_tracked_pages(self) -> List[Dict]:
        """동기화 추적 중인 모든 노션 페이지 조회

        Returns:
            [{"page_id": str, "chat_id": int, "msg_id": int,
              "title": str}, ...]
        """
        results = []
        has_more = True
        start_cursor = None

        while has_more:
            query_params = {
                "database_id": self.database_id,
                "page_size": 100,
                "filter": {
                    "property": "telegram_msg_id",
                    "number": {"is_not_empty": True},
                },
            }
            if start_cursor:
                query_params["start_cursor"] = start_cursor

            try:
                response = self.client.databases.query(
                    **query_params
                )
            except Exception as e:
                logger.error(f"추적 페이지 조회 실패: {e}")
                break

            for page in response.get("results", []):
                if page.get("archived"):
                    continue
                props = page.get("properties", {})

                chat_id = None
                msg_id = None
                title = ""

                if "telegram_chat_id" in props:
                    chat_id = props["telegram_chat_id"].get(
                        "number"
                    )
                if "telegram_msg_id" in props:
                    msg_id = props["telegram_msg_id"].get(
                        "number"
                    )

                title_prop = props.get("주소 및 상호", {})
                title_list = title_prop.get("title", [])
                if title_list:
                    title = (
                        title_list[0]
                        .get("text", {})
                        .get("content", "")
                    )

                if chat_id and msg_id:
                    results.append(
                        {
                            "page_id": page["id"],
                            "chat_id": int(chat_id),
                            "msg_id": int(msg_id),
                            "title": title,
                        }
                    )

            has_more = response.get("has_more", False)
            start_cursor = response.get("next_cursor")

        return results


class TelegramNotionBot:
    """텔레그램-노션 연동 봇 (앨범/여러 장 사진 + 원본 수정 자동 반영)"""

    # 앨범 사진 수집 대기 시간 (초)
    MEDIA_GROUP_TIMEOUT = 2.0
    # 복수 미디어그룹 수집 시간창 (초) - 이 시간 이내 사진들을 같은 매물로 묶음
    PROPERTY_COLLECT_WINDOW = 120
    # 저장 대기 버퍼 (초) - 매물 설명 감지 후 이 시간 후에 저장 (실수 삭제 방지)
    PROPERTY_SAVE_BUFFER = 30

    HELP_TEXT = (
        "🏠 *부동산 매물 등록 봇*\n\n"
        "사진과 함께 아래 형식으로 매물 정보를 보내주세요:\n\n"
        "```\n"
        "남구 대명동 1724\\-3 2층 일부\n"
        "1\\. 2000/110 부별\n"
        "2\\. 관리비 실비\n"
        "3\\. 무권리\n"
        "4\\. 2종근생 계약 178\\.66m2 / 전용 33\\.05m2\n"
        "5\\. 주차 매장앞1대 / 내부화장실 1개\n"
        "6\\. 남향\n"
        "7\\. 등기o / 대장o\n"
        "8\\. 양도인 010\\-1234\\-5678 / 임대인 010\\-9876\\-5432\n\n"
        "특이사항\n"
        "메모 내용\n"
        "```\n\n"
        "📌 *사용법:*\n"
        "• 사진 여러 장 \\+ 캡션 → 모든 사진 등록\n"
        "• 텍스트만 보내기 → 사진 없이 등록\n"
        "• 원본 메시지 수정 → 노션에 자동 반영 ✨\n\n"
        "📌 *수정 방법:*\n"
        "등록된 매물의 *원본 메시지를 직접 수정*하면\n"
        "노션에도 자동으로 반영됩니다\\!\n"
        "예: `1\\.3000/150 부별` → 보증금/월세/부가세 수정\n\n"
        "📌 *삭제 방법:*\n"
        "• 텔레그램에서 매물 메시지를 그냥 삭제하세요\\!\n"
        "  → 4시간마다 자동 동기화로 노션에서도 삭제됩니다 🔄\n"
        "• 즉시 삭제: `/동기화` 입력하면 바로 정리\n"
        "• 개별 삭제: 매물에 답장으로 `/delete` 입력\n\n"
        "📌 *명령어:*\n"
        "/start \\- 봇 시작\n"
        "/help \\- 도움말 보기\n"
        "/check \\- 매물 동기화 상태 확인 \\(간단\\)\n"
        "/매물확인 \\- 텔레그램↔노션 전체 매물 비교\n"
        "/동기화 \\- 삭제된 매물 노션 정리 \\(수동\\)\n"
        "/delete \\- 매물 개별 삭제 \\(답장으로 사용\\)"
    )

    def __init__(
        self,
        telegram_token: str,
        notion_token: str,
        database_id: str,
    ):
        self.telegram_token = telegram_token
        self.notion_uploader = NotionUploader(notion_token, database_id)
        self.parser = PropertyParser()
        # 미디어 그룹 버퍼
        self._media_groups: Dict[str, Dict] = {}
        # asyncio 타이머 태스크
        self._pending_tasks: Dict[str, asyncio.Task] = {}
        # 메시지 ID → 노션 페이지 ID 매핑
        self._page_mapping: Dict[int, str] = {}
        # 메시지 ID → 원본 매물 텍스트 (변경 감지용)
        self._original_texts: Dict[int, str] = {}
        # 메시지 ID → 채팅 ID (동기화 시 메시지 존재 확인용)
        self._msg_chat_ids: Dict[int, int] = {}
        # 동기화 중 플래그 (전달 메시지 무시용)
        self._sync_in_progress = False
        # 채팅별 사진 수집 버퍼 (복수 미디어그룹 + 분리 텍스트 묶음 처리)
        self._chat_buffers: Dict[int, Dict] = {}
        # 30초 저장 대기 태스크 (실수 삭제 방지 버퍼)
        self._save_tasks: Dict[int, asyncio.Task] = {}
        # 2분 버퍼 만료 태스크
        self._collect_tasks: Dict[int, asyncio.Task] = {}
        # 추가사진 버퍼: {orig_msg_id: {"photos": [], "label": str, "page_id": str, "timer_task": Task}}
        self._extra_photo_buffers: Dict[int, Dict] = {}

        # 매물접수자 이름 목록 (노션 셀렉트 옵션과 일치해야 함)
        self._staff_names = [
            "박진우", "김동영", "임정묵",
            "김태훈", "한지훈", "허종찬", "고동기",
        ]

        # 동기화용 Notion 속성 초기화
        self.notion_uploader.ensure_sync_properties()

    @staticmethod
    def _normalize_korean_name(name: str) -> str:
        """한국 이름 순서 정규화

        텔레그램 서명은 프로필 설정에 따라 "이름 성" 순서로 오는 경우가 있음.
        예: "진우 박" → "박진우",  "도희 김" → "김도희"
            "박 진우" → "박진우"  (올바른 순서, 공백만 제거)

        규칙:
          - 단어 2개 + 마지막이 1글자 → 성이 뒤에 온 것 → 앞뒤 교체
          - 단어 2개 + 첫 번째가 1글자 → 이미 "성 이름" → 공백만 제거
          - 그 외                       → 공백 제거 후 그대로 사용
        """
        parts = name.strip().split()
        if len(parts) == 2:
            if len(parts[-1]) == 1:
                # "진우 박" → "박진우"
                return parts[-1] + parts[0]
            elif len(parts[0]) == 1:
                # "박 진우" → "박진우"
                return parts[0] + parts[1]
        # 공백 모두 제거
        return re.sub(r"\s+", "", name)

    def _match_staff_name(self, signature: Optional[str]) -> Optional[str]:
        """채널 서명에서 매물접수자 이름 매칭

        Args:
            signature: message.author_signature 값
                       (텔레그램 프로필 이름 형식: "진우 박" 또는 "박진우" 등)

        Returns:
            정규화된 이름 (예: "박진우") 또는 None
        """
        if not signature:
            logger.debug("author_signature가 없음")
            return None

        sig = signature.strip()
        # 한국 이름 순서 정규화 ("진우 박" → "박진우")
        sig_norm = self._normalize_korean_name(sig)
        logger.info(f"서명 매칭 시도: '{sig}' → 정규화: '{sig_norm}'")

        for name in self._staff_names:
            name_norm = re.sub(r"\s+", "", name)
            if name_norm == sig_norm or name_norm in sig_norm or sig_norm in name_norm:
                logger.info(f"매칭 성공: '{sig}' → '{name}'")
                return name

        # 미리 등록된 이름과 매칭 안 되면 정규화된 서명을 그대로 사용
        logger.info(f"이름 목록 미매칭, 정규화 서명 저장: '{sig_norm}'")
        return sig_norm[:30] if sig_norm else None

    @staticmethod
    def _is_listing_format(
        text: str, is_update: bool = False
    ) -> bool:
        """매물 형식 메시지인지 판별 (1. 2. 3. 등 번호 형식)

        Args:
            text: 검사할 텍스트
            is_update: 수정 모드 (True 시 최소 길이 완화)
        """
        if not text:
            return False
        text = text.strip()
        # 수정 모드: 최소 3글자 (예: "3.3000")
        # 신규 등록: 최소 10글자 (사적 대화 방지)
        min_len = 3 if is_update else 10
        if len(text) < min_len:
            return False
        # 번호 형식 (1.~8.) 체크
        if any(f"{i}." in text for i in range(1, 9)):
            return True
        # 수정 모드에서는 "특이사항" 키워드도 허용
        if is_update and "특이사항" in text:
            return True
        return False

    # ──────────────────────────────────────────────
    # ✅ 원본 메시지 수정 헬퍼 (구분선 추가)
    # ──────────────────────────────────────────────

    DIVIDER = "━━━━━━━━━━━━━━"  # 구분선

    @staticmethod
    async def _safe_edit_message(
        message, property_text: str, notion_section_html: str,
        notion_section_plain: str, is_caption: bool = False,
    ):
        """HTML 모드로 메시지 수정 시도 → 실패 시 plain text fallback

        Args:
            message: 텔레그램 메시지 객체
            property_text: 매물 정보 원본 텍스트
            notion_section_html: HTML 하이퍼링크 포함 노션 섹션
            notion_section_plain: plain text 노션 섹션 (fallback)
            is_caption: True면 edit_caption, False면 edit_text
        """
        # HTML 모드: 매물 텍스트를 이스케이프하고 노션 섹션은 HTML 유지
        escaped_text = html.escape(property_text)
        html_full = escaped_text + notion_section_html

        try:
            if is_caption:
                await message.edit_caption(
                    caption=html_full, parse_mode="HTML"
                )
            else:
                await message.edit_text(
                    html_full, parse_mode="HTML"
                )
            return True
        except Exception as e:
            logger.warning(f"HTML 모드 실패, plain text로 전환: {e}")
            # Fallback: plain text (기존 방식)
            plain_full = property_text + notion_section_plain
            try:
                if is_caption:
                    await message.edit_caption(caption=plain_full)
                else:
                    await message.edit_text(plain_full)
                return True
            except Exception as e2:
                logger.error(f"메시지 수정 실패: {e2}")
                return False

    @staticmethod
    def _extract_property_text(message_text: str) -> str:
        """메시지에서 구분선 위쪽(매물 정보)만 추출"""
        if TelegramNotionBot.DIVIDER in message_text:
            return message_text.split(TelegramNotionBot.DIVIDER)[0].strip()
        return message_text.strip()

    @staticmethod
    def _build_notion_section(
        page_url: str, page_id: str, update_log: str = "",
        use_html: bool = True,
    ) -> str:
        """구분선 아래 노션 정보 섹션 생성

        Args:
            page_url: 노션 페이지 URL
            page_id: 노션 페이지 ID
            update_log: 수정 이력 문자열
            use_html: True면 HTML 하이퍼링크, False면 plain text
        """
        if use_html:
            section = (
                f"\n\n{TelegramNotionBot.DIVIDER}\n"
                f'✅ <a href="{page_url}">Notion</a>'
            )
        else:
            section = (
                f"\n\n{TelegramNotionBot.DIVIDER}\n"
                f"✅ Notion\n"
                f"🔗 {page_url}"
            )
        if update_log:
            section += f"\n{update_log}"
        return section

    @staticmethod
    def _build_update_summary(
        old_data: Dict, new_data: Dict
    ) -> str:
        """수정 사항을 한 줄로 간략하게 요약
        예: 월세55→65, 보증금1000→2000
        """
        changes = []
        field_names = {
            "주소": "주소",
            "층수": "층수",
            "보증금": "보증금",
            "월세": "월세",
            "부가세": "부가세",
            "관리비": "관리비",
            "권리금": "권리금",
            "건축물용도": "용도",
            "계약면적": "계약㎡",
            "전용면적": "전용㎡",
            "주차": "주차",
            "방향": "방향",
            "화장실 위치": "화장실위치",
            "화장실 수": "화장실",
            "위반건축물": "위반",
            "대표 연락처": "연락처",
            "매물_유형": "매물유형",
            "소재지_구": "소재지",
            "임대_구분": "임대구분",
        }
        
        for key, label in field_names.items():
            if key not in new_data:
                continue
            new_val = new_data[key]
            old_val = old_data.get(key)

            # 리스트(multi_select) 타입 처리 (건축물용도 등)
            def _to_str(v):
                if isinstance(v, list):
                    return ", ".join(str(x) for x in v)
                return str(v) if v is not None else ""

            # 숫자 비교
            if isinstance(old_val, (int, float)) and isinstance(new_val, (int, float)):
                if old_val != new_val:
                    old_disp = int(old_val) if isinstance(old_val, float) and old_val == int(old_val) else old_val
                    changes.append(f"{label}{old_disp}→{new_val}")
            elif old_val is not None:
                if _to_str(old_val) != _to_str(new_val):
                    changes.append(f"{label}{_to_str(old_val)}→{_to_str(new_val)}")
            else:
                # 새로 추가
                changes.append(f"{label}:{_to_str(new_val)}")
        
        # 거래 상태 체크 (특별 처리)
        if "거래_상태" in new_data:
            old_status = old_data.get("거래_상태")
            new_status = new_data["거래_상태"]
            if old_status != new_status:
                # 거래완료 시점도 함께 표시
                if "거래완료_시점" in new_data:
                    changes.append(f"거래완료({new_data['거래완료_시점']})")
                else:
                    changes.append(f"거래상태:{new_status}")
        
        # 특이사항 체크
        if "특이사항" in new_data:
            if str(old_data.get("특이사항", "")) != str(new_data["특이사항"]):
                changes.append("특이사항수정")
        
        return ", ".join(changes) if changes else "내용수정"

    # ──────────────────────────────────────────────
    # 채팅 버퍼 & 저장 버퍼 관리
    # (복수 미디어그룹 + 사진/텍스트 분리 업로드 지원)
    # ──────────────────────────────────────────────

    def _get_or_create_buffer(self, chat_id: int) -> Dict:
        """채팅별 사진 버퍼 가져오기 (없으면 생성)"""
        if chat_id not in self._chat_buffers:
            self._chat_buffers[chat_id] = {
                # 층별 사진 그룹: [{"label": "1층"|None, "photos": [...]}]
                "floor_groups": [{"label": None, "photos": []}],
                "first_message": None,
                "author_signature": None,
            }
        return self._chat_buffers[chat_id]

    def _add_photos_to_buffer(
        self,
        chat_id: int,
        photos: List[str],
        message,
        author_sig: str = None,
    ):
        """채팅 버퍼에 사진 추가 (현재 floor_group의 마지막 그룹에 추가) + 2분 만료 타이머 리셋"""
        buf = self._get_or_create_buffer(chat_id)

        # floor_groups 마지막 그룹에 사진 추가
        floor_groups = buf.setdefault(
            "floor_groups", [{"label": None, "photos": []}]
        )
        floor_groups[-1]["photos"].extend(photos)

        if buf["first_message"] is None:
            buf["first_message"] = message
        if author_sig and not buf.get("author_signature"):
            buf["author_signature"] = author_sig
        # 기존 만료 태스크 취소 후 재시작
        existing = self._collect_tasks.get(chat_id)
        if existing:
            existing.cancel()
        self._collect_tasks[chat_id] = asyncio.create_task(
            self._expire_chat_buffer(chat_id)
        )

    def _add_floor_label_to_buffer(self, chat_id: int, label: str):
        """버퍼에 층수 라벨 추가 → 사진 그룹 구분

        사진 뒤에 라벨이 오는 경우 (가장 일반적):
            [사진 10장] → "1층" → [사진 12장] → "2층" → [매물설명]
        사진 앞에 라벨이 오는 경우도 지원:
            "1층" → [사진 10장] → "2층" → [사진 12장] → [매물설명]
        """
        buf = self._chat_buffers.get(chat_id)
        if not buf:
            return

        floor_groups = buf.get(
            "floor_groups", [{"label": None, "photos": []}]
        )
        if not floor_groups:
            floor_groups = [{"label": None, "photos": []}]
            buf["floor_groups"] = floor_groups

        last_group = floor_groups[-1]

        if last_group["photos"]:
            # 사진이 먼저 왔음 → 현재 그룹에 라벨 붙이기
            if last_group["label"] is None:
                last_group["label"] = label
            # 다음 사진을 위한 새 그룹 생성
            floor_groups.append({"label": None, "photos": []})
        else:
            # 사진 없이 라벨만 왔음 → 이 라벨로 다음 사진 그룹 미리 지정
            last_group["label"] = label

        logger.debug(
            f"층수 라벨 추가: '{label}', "
            f"floor_groups={len(floor_groups)}개 (chat_id={chat_id})"
        )

    async def _expire_chat_buffer(self, chat_id: int):
        """2분 후 채팅 버퍼 자동 만료 (매물 설명 없으면 사진 폐기)"""
        await asyncio.sleep(self.PROPERTY_COLLECT_WINDOW)
        self._chat_buffers.pop(chat_id, None)
        self._collect_tasks.pop(chat_id, None)
        logger.debug(f"채팅 버퍼 만료: chat_id={chat_id}")

    def _clear_chat_buffer(self, chat_id: int):
        """채팅 버퍼 즉시 정리"""
        self._chat_buffers.pop(chat_id, None)
        task = self._collect_tasks.pop(chat_id, None)
        if task:
            task.cancel()

    async def _schedule_property_save(
        self,
        chat_id: int,
        description: str,
        trigger_message,
        context,
    ):
        """30초 후 매물 저장 예약 (실수 삭제 방지 버퍼)"""
        # 기존 저장 태스크 취소 (같은 채팅에서 새 매물 설명이 오면 덮어쓰기)
        existing = self._save_tasks.get(chat_id)
        if existing:
            existing.cancel()
        self._save_tasks[chat_id] = asyncio.create_task(
            self._do_save_with_buffer(
                chat_id, description, trigger_message, context.bot
            )
        )
        logger.debug(
            f"매물 저장 예약: chat_id={chat_id}, "
            f"{self.PROPERTY_SAVE_BUFFER}초 후 실행"
        )

    async def _do_save_with_buffer(
        self,
        chat_id: int,
        description: str,
        trigger_message,
        bot,
    ):
        """30초 대기 → 트리거 메시지 존재 확인 → 저장 실행"""
        await asyncio.sleep(self.PROPERTY_SAVE_BUFFER)
        self._save_tasks.pop(chat_id, None)

        # 트리거 메시지(매물 설명) 존재 확인 (30초 이내 삭제 시 저장 취소)
        exists = await self._check_message_exists(
            bot, trigger_message.chat_id, trigger_message.message_id
        )
        if not exists:
            logger.info(
                f"트리거 메시지 삭제됨, 저장 취소: chat_id={chat_id}"
            )
            self._clear_chat_buffer(chat_id)
            return

        # 버퍼에서 사진 & 층별 그룹 가져오기
        buf = self._chat_buffers.get(chat_id, {})
        floor_groups = buf.get("floor_groups", [])
        author_sig = buf.get("author_signature") or getattr(
            trigger_message, "author_signature", None
        )

        # 전체 사진 URL 목록 (flat)
        photo_urls: List[str] = []
        for g in floor_groups:
            photo_urls.extend(g.get("photos", []))

        # 층 구분 여부: 하나 이상의 그룹에 라벨이 있으면 floor_photos 전달
        has_floor_structure = any(
            g.get("label") for g in floor_groups
        )
        floor_photos_arg = floor_groups if has_floor_structure else None

        # 버퍼 정리 (중복 저장 방지)
        self._clear_chat_buffer(chat_id)

        # 매물 저장 실행
        await self._save_property_to_notion(
            description, trigger_message, photo_urls, author_sig,
            floor_photos=floor_photos_arg,
        )

    # ──────────────────────────────────────────────
    # 답장(Reply) 기반 매물 수정 기능
    # ──────────────────────────────────────────────

    @staticmethod
    def _parse_deal_complete(text: str) -> Tuple[bool, Optional[str]]:
        """거래완료/계약완료 답장 패턴 감지 및 담당자 이름 추출

        인식 패턴 (괄호 종류·공백 무관):
            (계약완료), [거래완료]
            (계약완료 박진우), [거래완료 김동영]
            (계약 완료 박진우), (거래완료박진우)

        Returns:
            (is_deal_complete, agent_name_or_None)
        """
        if not text:
            return False, None
        m = re.search(
            r'[\(\[]\s*(?:계약|거래)\s*완료\s*([^\)\]]*)\s*[\)\]]',
            text,
        )
        if m:
            agent_raw = m.group(1).strip()
            # 공백 정규화 (앞뒤 공백 제거, 내부 다중 공백 단일화)
            agent_clean = re.sub(r'\s+', ' ', agent_raw).strip()
            return True, agent_clean if agent_clean else None
        return False, None

    async def _handle_deal_complete_reply(
        self,
        message,
        context,
        agent_name: Optional[str],
    ):
        """거래완료 답장 처리 → 노션 '거래 상태' 업데이트

        Args:
            message: 답장 메시지 객체
            context: 텔레그램 컨텍스트
            agent_name: 계약 담당자 이름 (없으면 None)
        """
        reply = message.reply_to_message
        if not reply:
            return

        page_id = self._get_page_id_from_reply(reply)
        if not page_id:
            logger.debug(
                f"거래완료 답장: 연결된 노션 페이지 없음 "
                f"(msg_id={reply.message_id})"
            )
            return

        success = self.notion_uploader.update_deal_status(
            page_id, agent_name
        )
        if success:
            result_msg = "✅ 거래 완료 처리됐습니다."
            if agent_name:
                result_msg += f"\n👤 계약담당자: {agent_name}"
            try:
                await message.reply_text(result_msg)
            except Exception:
                pass
        else:
            try:
                await message.reply_text(
                    "⚠️ 거래완료 처리 중 오류가 발생했습니다."
                )
            except Exception:
                pass

    def _get_page_id_from_reply(
        self, reply_message
    ) -> Optional[str]:
        """답장 대상 메시지에서 노션 페이지 ID 추출
        (원본 매물 게시물 지원)
        """
        msg_id = reply_message.message_id

        # 1. 저장된 매핑에서 찾기
        if msg_id in self._page_mapping:
            return self._page_mapping[msg_id]

        # 2. 텍스트 또는 entities에서 Notion URL 추출
        text = reply_message.text or reply_message.caption or ""
        notion_url = ""
        if "notion.so" in text:
            notion_url = text
        
        # HTML 하이퍼링크에서 URL 추출
        entities = (
            reply_message.entities
            or reply_message.caption_entities
            or []
        )
        for ent in entities:
            if ent.type == "text_link" and ent.url and "notion.so" in ent.url:
                notion_url = ent.url
                break
        
        if notion_url:
            match = re.search(r'([a-f0-9]{32})', notion_url)
            if match:
                raw_id = match.group(1)
                page_id = (
                    f"{raw_id[:8]}-{raw_id[8:12]}"
                    f"-{raw_id[12:16]}"
                    f"-{raw_id[16:20]}-{raw_id[20:]}"
                )
                return page_id

        return None

    @staticmethod
    def _parse_change_section(
        section_text: str,
    ) -> Dict[str, str]:
        """수정 섹션 텍스트에서 {필드라벨: 변경이력} 추출
        (이 함수는 더 이상 사용하지 않음 - 답장 수정 방식 제거)
        """
        return {}

    async def handle_edited_message(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ):
        """채널 메시지 수정 감지 및 노션 자동 업데이트"""
        # 수정된 메시지가 아니면 무시 (group=1에서 모든 업데이트를 받으므로)
        if not (update.edited_channel_post or update.edited_message):
            return
        
        message = update.effective_message
        if not message:
            return
        
        msg_id = message.message_id
        current_text = message.text or message.caption or ""
        
        # 매핑된 페이지가 없으면 메시지에서 복구 시도
        if msg_id not in self._page_mapping:
            if self.DIVIDER not in current_text:
                return
            
            # 1) 텍스트에서 노션 URL 찾기 (plain text fallback 경우)
            notion_url = ""
            if "notion.so" in current_text:
                notion_url = current_text
            
            # 2) entities에서 text_link 찾기 (HTML 하이퍼링크 경우)
            entities = message.entities or message.caption_entities or []
            for ent in entities:
                if ent.type == "text_link" and ent.url and "notion.so" in ent.url:
                    notion_url = ent.url
                    break
            
            if not notion_url:
                return
            
            match = re.search(r'([a-f0-9]{32})', notion_url)
            if match:
                raw_id = match.group(1)
                page_id = (
                    f"{raw_id[:8]}-{raw_id[8:12]}"
                    f"-{raw_id[12:16]}"
                    f"-{raw_id[16:20]}-{raw_id[20:]}"
                )
                self._page_mapping[msg_id] = page_id
                self._msg_chat_ids[msg_id] = message.chat_id
                logger.info(
                    f"매핑 복구: msg_id={msg_id} → {page_id}"
                )
            else:
                return
        
        page_id = self._page_mapping[msg_id]
        
        # 구분선으로 매물 정보만 추출
        property_text = self._extract_property_text(current_text)
        
        # 이전 매물 텍스트와 비교 (매핑 복구 시 이전 텍스트 없으면 무조건 업데이트)
        old_property_text = self._original_texts.get(msg_id, "")
        
        # 거래 완료 체크 (전체 메시지에서 체크 - 구분선 아래 포함)
        current_text_no_space = current_text.replace(" ", "").replace("\n", "")
        has_deal_completed = "거래완료" in current_text_no_space or "계약완료" in current_text_no_space
        
        # 변경 없고 거래완료도 없으면 무시
        if property_text == old_property_text and not has_deal_completed:
            logger.debug(f"매물 정보 변경 없음: msg_id={msg_id}")
            return
        
        # 매물 형식인지 확인 (거래완료만 있는 경우는 패스)
        if property_text != old_property_text and not self._is_listing_format(property_text, is_update=True):
            return
        
        logger.info(f"매물 수정 감지: msg_id={msg_id}")
        
        try:
            # 기존 노션 데이터 조회
            old_data = self.notion_uploader.get_page_properties(page_id)
            
            # 수정된 매물 정보 파싱 (주소 포함)
            new_property_data = {}
            if property_text != old_property_text:
                # 매물 정보가 변경된 경우에만 파싱
                new_property_data = self.parser.parse_property_info(
                    property_text, skip_address=False
                )
                if not new_property_data:
                    new_property_data = {}
                # 특이사항 추가 모드는 원본 수정에서는 지원 안 함
                new_property_data.pop("특이사항_추가", None)
            
            # 거래 완료 처리 (구분선 위/아래 모두 체크)
            if has_deal_completed:
                # 이전에 거래완료가 아니었다면 새로 거래완료 처리
                if old_data.get("거래_상태") != "거래 완료":
                    new_property_data["거래_상태"] = "거래 완료"
                    # 거래완료 시점 기록
                    now = datetime.now()
                    new_property_data["거래완료_시점"] = now.strftime("%Y-%m-%d %H:%M")
                    logger.info(f"거래 완료 감지: msg_id={msg_id}, 시점={new_property_data['거래완료_시점']}")
            
            # 노션에 업데이트할 내용이 없으면 종료
            if not new_property_data:
                return
            
            # 노션 업데이트
            page_url = self.notion_uploader.update_property(
                page_id, new_property_data
            )
            
            # 변경 요약 생성
            summary = self._build_update_summary(old_data, new_property_data)
            now = datetime.now().strftime("%m/%d %H:%M")
            update_log = f"🔄 {now} {summary}"
            
            # 기존 수정 이력 유지
            existing_logs = ""
            if self.DIVIDER in current_text:
                below_divider = current_text.split(self.DIVIDER, 1)[1]
                for line in below_divider.split("\n"):
                    if line.strip().startswith("🔄"):
                        existing_logs += f"\n{line.strip()}"
            
            # 원본 메시지에 수정 이력 추가
            all_logs = update_log
            if existing_logs:
                all_logs += existing_logs
            
            notion_html = self._build_notion_section(
                page_url, page_id, all_logs, use_html=True
            )
            notion_plain = self._build_notion_section(
                page_url, page_id, all_logs, use_html=False
            )
            
            # 현재 텍스트를 저장 (다음 비교용) - 수정 전에 저장
            self._original_texts[msg_id] = property_text
            
            # 메시지 수정 (HTML 시도 → 실패 시 plain text)
            is_caption = message.caption is not None
            await self._safe_edit_message(
                message, property_text,
                notion_html, notion_plain,
                is_caption=is_caption,
            )
            
            logger.info(f"매물 자동 수정 완료: {summary}")
            
        except Exception as e:
            logger.error(f"메시지 수정 처리 오류: {e}", exc_info=True)

    async def _handle_update(
        self, message, page_id: str, context
    ):
        """답장 메시지로 노션 매물 정보 수정 (더 이상 사용하지 않음)"""
        # 원본 수정으로 대체되었으므로 사용하지 않음
        await message.reply_text(
            "💡 원본 메시지를 직접 수정하면 노션에도 자동 반영됩니다!"
        )
        return

    # ──────────────────────────────────────────────
    # 명령어 핸들러
    # ──────────────────────────────────────────────

    async def start_command(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ):
        message = update.effective_message
        if message:
            await message.reply_text(
                "👋 안녕하세요\\! 부동산 매물 등록 봇입니다\\.\n\n"
                "사진과 매물 정보를 보내주시면 자동으로 노션에 등록합니다\\.\n"
                "원본 메시지를 수정하면 노션에도 자동 반영됩니다\\!\n\n"
                "/help 로 사용법을 확인하세요\\!",
                parse_mode="MarkdownV2",
            )

    async def help_command(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ):
        message = update.effective_message
        if message:
            await message.reply_text(
                self.HELP_TEXT, parse_mode="MarkdownV2"
            )

    async def property_check_command(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ):
        """/매물확인 명령어: 텔레그램 vs 노션 매물 차이 확인"""
        message = update.effective_message
        if not message:
            return

        try:
            status_msg = await message.reply_text(
                "🔍 매물 확인 중...\n"
                "텔레그램과 노션을 비교합니다..."
            )

            # ── 1단계: 노션에서 추적 중인 모든 매물 조회 ──
            tracked_pages = self.notion_uploader.get_tracked_pages()
            notion_map = {}  # {msg_id: {"page_id": ..., "title": ...}}
            for page in tracked_pages:
                notion_map[page["msg_id"]] = {
                    "page_id": page["page_id"],
                    "title": page["title"],
                    "chat_id": page["chat_id"],
                }

            # ── 2단계: 메모리 매핑 추가 (봇이 업로드한 매물) ──
            all_msg_ids = set(notion_map.keys()) | set(self._page_mapping.keys())

            telegram_exists = {}  # {msg_id: bool}
            notion_only = []  # [(title, page_id)]
            telegram_only = []  # [(msg_id, title)]

            # ── 3단계: 각 메시지 존재 여부 확인 ──
            checked = 0
            for msg_id in all_msg_ids:
                checked += 1
                
                # chat_id 찾기
                chat_id = None
                if msg_id in notion_map:
                    chat_id = notion_map[msg_id]["chat_id"]
                elif msg_id in self._msg_chat_ids:
                    chat_id = self._msg_chat_ids[msg_id]
                else:
                    chat_id = message.chat_id  # 기본값

                # 텔레그램 메시지 존재 확인
                exists = await self._check_message_exists(
                    context.bot, chat_id, msg_id
                )
                telegram_exists[msg_id] = exists

                # API 속도 제한 방지
                await asyncio.sleep(0.05)

                # 진행 상황 업데이트 (50개마다)
                if checked % 50 == 0:
                    await status_msg.edit_text(
                        f"🔍 매물 확인 중... {checked}/{len(all_msg_ids)}"
                    )

            # ── 4단계: 차이점 분석 ──
            for msg_id in all_msg_ids:
                exists = telegram_exists.get(msg_id, False)
                in_notion = msg_id in notion_map
                in_memory = msg_id in self._page_mapping

                if not exists and in_notion:
                    # 텔레그램에 없는데 노션에 있음 → 노션에만 있음
                    notion_only.append(
                        (notion_map[msg_id]["title"], notion_map[msg_id]["page_id"])
                    )
                elif exists and not in_notion and not in_memory:
                    # 텔레그램에 있는데 노션/메모리에 없음 → 텔레그램에만 있음
                    telegram_only.append((msg_id, f"msg_{msg_id}"))

            # ── 5단계: 결과 메시지 생성 ──
            telegram_count = sum(1 for exists in telegram_exists.values() if exists)
            notion_count = len(notion_map) + len(
                [m for m in self._page_mapping if m not in notion_map]
            )

            result = "📊 매물 확인 결과\n"
            result += "━━━━━━━━━━━━━━━━\n"
            result += f"📱 텔레그램 매물: {telegram_count}개\n"
            result += f"📝 노션 매물: {notion_count}개\n\n"

            if notion_only:
                result += f"❌ 노션에만 있는 매물: {len(notion_only)}개\n"
                result += "(텔레그램에서 삭제됨)\n"
                result += "━━━━━━━━━━━━━━━━\n\n"
                for title, page_id in notion_only[:10]:
                    result += f"{title}\n"
                if len(notion_only) > 10:
                    result += f"... (외 {len(notion_only) - 10}개)\n"
                result += "\n💡 조치 방법:\n"
                result += "→ /동기화 실행하면 노션에서 자동 삭제됩니다.\n\n"

            if telegram_only:
                result += f"❌ 텔레그램에만 있는 매물: {len(telegram_only)}개\n"
                result += "(노션에 등록 안됨)\n"
                result += "━━━━━━━━━━━━━━━━\n\n"
                for msg_id, title in telegram_only[:10]:
                    result += f"{title} (msg_id: {msg_id})\n"
                if len(telegram_only) > 10:
                    result += f"... (외 {len(telegram_only) - 10}개)\n"
                result += "\n💡 조치 방법:\n"
                result += "1. 노션 휴지통에서 복원\n"
                result += "2. 또는 텔레그램에서 해당 메시지 수정\n"
                result += "   (아무 글자 추가/삭제하면 봇이 자동 재등록)\n\n"

            if not notion_only and not telegram_only:
                result += "✅ 완벽하게 동기화되어 있습니다!\n"
                result += "텔레그램과 노션의 매물이 일치합니다.\n"

            await status_msg.edit_text(result)

        except Exception as e:
            logger.error(f"/매물확인 오류: {e}", exc_info=True)
            await message.reply_text(f"❌ 확인 중 오류 발생: {str(e)}")

    async def check_command(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ):
        """텔레그램 매물과 노션 매물 동기화 체크 (기존 간단 버전)"""
        message = update.effective_message
        if not message:
            return
        
        try:
            status_msg = await message.reply_text(
                "⏳ 노션 매물 확인 중...\n"
                "(텔레그램 메시지는 메모리에 있는 것만 확인됩니다)"
            )
            
            # 현재 메모리에 있는 텔레그램 매물 (봇 실행 후 등록된 것들)
            telegram_properties = {}  # {주소: 메시지ID}
            
            for msg_id, page_id in self._page_mapping.items():
                if msg_id in self._original_texts:
                    text = self._original_texts[msg_id]
                    lines = text.strip().split("\n")
                    if lines:
                        address = lines[0].strip()
                        telegram_properties[address] = msg_id
            
            # 노션 데이터베이스에서 모든 매물 주소 수집
            notion_properties = {}  # {주소: 페이지ID}
            
            has_more = True
            start_cursor = None
            
            while has_more:
                query_params = {
                    "database_id": self.notion_uploader.database_id,
                    "page_size": 100,
                }
                if start_cursor:
                    query_params["start_cursor"] = start_cursor
                
                response = self.notion_uploader.client.databases.query(
                    **query_params
                )
                
                for page in response.get("results", []):
                    props = page.get("properties", {})
                    title_prop = props.get("주소 및 상호", {})
                    title_list = title_prop.get("title", [])
                    
                    if title_list:
                        address = title_list[0].get("text", {}).get("content", "")
                        if address:
                            notion_properties[address] = page["id"]
                
                has_more = response.get("has_more", False)
                start_cursor = response.get("next_cursor")
            
            # 비교 결과 생성
            telegram_count = len(telegram_properties)
            notion_count = len(notion_properties)
            
            telegram_addrs = set(telegram_properties.keys())
            notion_addrs = set(notion_properties.keys())
            
            missing_in_notion = telegram_addrs - notion_addrs
            missing_in_telegram = notion_addrs - telegram_addrs
            
            # 결과 메시지 생성
            result_text = "📊 매물 동기화 체크 결과\n\n"
            result_text += f"📱 텔레그램 매물 (봇 실행 후): {telegram_count}개\n"
            result_text += f"📝 노션 매물 (전체): {notion_count}개\n"
            
            if missing_in_notion:
                result_text += f"\n⚠️ 노션에 없는 매물 ({len(missing_in_notion)}개):\n"
                for addr in sorted(missing_in_notion)[:10]:
                    result_text += f"  • {addr}\n"
                if len(missing_in_notion) > 10:
                    result_text += f"  ... 외 {len(missing_in_notion) - 10}개\n"
            
            if telegram_count > 0:
                sync_rate = len(telegram_addrs & notion_addrs) / telegram_count * 100
                result_text += f"\n✅ 동기화율: {sync_rate:.1f}%\n"
            
            if not missing_in_notion and telegram_count > 0:
                result_text += "\n✅ 봇 실행 후 등록된 모든 매물이 동기화되어 있습니다!"
            elif telegram_count == 0:
                result_text += "\n💡 봇 실행 후 등록된 매물이 없습니다.\n"
                result_text += f"   (노션에는 총 {notion_count}개 매물이 있습니다)"
            else:
                result_text += "\n💡 동기화되지 않은 매물을 확인하세요."
            
            result_text += "\n\n⚠️ 참고: 봇 실행 전 매물은 표시되지 않습니다."
            
            await status_msg.edit_text(result_text)
            
        except Exception as e:
            logger.error(f"매물 체크 오류: {e}", exc_info=True)
            await message.reply_text(
                f"❌ 체크 중 오류 발생: {str(e)}"
            )

    # ──────────────────────────────────────────────
    # 매물 삭제 (텔레그램 + 노션)
    # ──────────────────────────────────────────────

    async def delete_command(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ):
        """매물 삭제: 원본 매물 메시지에 답장으로 /delete 입력 시
        노션 페이지를 아카이브하고 텔레그램 메시지도 삭제"""
        message = update.effective_message
        if not message:
            return

        # 답장 대상 메시지 확인
        reply = message.reply_to_message
        if not reply:
            await message.reply_text(
                "💡 삭제할 매물 메시지에 **답장(Reply)**으로 "
                "/delete 를 입력해주세요.",
                parse_mode="Markdown",
            )
            return

        # 답장 대상에서 노션 페이지 ID 추출
        page_id = self._get_page_id_from_reply(reply)
        if not page_id:
            await message.reply_text(
                "⚠️ 이 메시지에 연결된 노션 페이지를 찾을 수 없습니다.\n"
                "노션에 등록된 매물 메시지에만 사용할 수 있습니다."
            )
            return

        try:
            # 노션 페이지 제목 조회 (확인용)
            page_props = self.notion_uploader.get_page_properties(page_id)
            page_title = page_props.get("주소", "매물")

            # 노션 페이지 아카이브
            self.notion_uploader.archive_property(page_id)

            # 매핑 정보 제거
            reply_id = reply.message_id
            self._page_mapping.pop(reply_id, None)
            self._original_texts.pop(reply_id, None)
            self._msg_chat_ids.pop(reply_id, None)

            # 원본 매물 메시지 삭제 시도
            deleted_msg = False
            try:
                await reply.delete()
                deleted_msg = True
            except Exception as e:
                logger.warning(
                    f"텔레그램 메시지 삭제 실패 (권한 부족): {e}"
                )

            # /delete 명령어 메시지도 삭제 시도
            try:
                await message.delete()
            except Exception:
                pass

            # 결과 알림 (명령어 메시지 삭제 실패 시에만 표시)
            if deleted_msg:
                # 두 메시지 모두 삭제된 경우 → 알림 없이 깔끔하게 처리
                # (만약 /delete 메시지 삭제 실패 시 아래 로그만 남김)
                logger.info(
                    f"매물 삭제 완료: '{page_title}' "
                    f"(page_id={page_id})"
                )
            else:
                # 텔레그램 메시지 삭제 실패 시 알림
                await message.reply_text(
                    f"✅ 노션에서 삭제 완료: {page_title}\n"
                    f"⚠️ 텔레그램 메시지는 수동으로 삭제해주세요.\n"
                    f"(봇에 메시지 삭제 권한이 필요합니다)"
                )

        except Exception as e:
            logger.error(f"매물 삭제 오류: {e}", exc_info=True)
            await message.reply_text(
                f"❌ 삭제 중 오류 발생: {str(e)}"
            )

    # ──────────────────────────────────────────────
    # 매물 노션 저장 (공통 로직)
    # ──────────────────────────────────────────────

    async def _save_property_to_notion(
        self,
        description: str,
        trigger_message,
        photo_urls: List[str],
        author_sig: str = None,
        floor_photos: Optional[List[Dict]] = None,
    ):
        """매물 정보를 노션에 저장하고 원본 메시지에 노션 링크 추가

        Args:
            description: 매물 설명 텍스트
            trigger_message: 노션 링크를 추가할 기준 메시지
            photo_urls: 전체 사진 URL 목록 (없으면 빈 리스트)
            author_sig: 작성자 서명 (author_signature)
            floor_photos: 층별 사진 그룹 [{"label": "1층", "photos": [...]}]
                          None이면 구분 없이 flat 표시
        """
        try:
            property_data = self.parser.parse_property_info(description)
            property_data["원본 메시지"] = description
            property_data["telegram_chat_id"] = trigger_message.chat_id
            property_data["telegram_msg_id"] = trigger_message.message_id

            # 채널 서명에서 매물접수자 추출
            sig = author_sig or getattr(
                trigger_message, "author_signature", None
            )
            staff = self._match_staff_name(sig)
            if staff:
                property_data["매물접수"] = staff

            # 노션 업로드
            page_url, page_id = self.notion_uploader.upload_property(
                property_data,
                photo_urls if photo_urls else None,
                floor_photos=floor_photos,
            )

            # 매핑 저장
            self._page_mapping[trigger_message.message_id] = page_id
            self._original_texts[trigger_message.message_id] = description
            self._msg_chat_ids[trigger_message.message_id] = (
                trigger_message.chat_id
            )

            # 원본 메시지에 노션 링크 추가
            notion_html = self._build_notion_section(
                page_url, page_id, use_html=True
            )
            notion_plain = self._build_notion_section(
                page_url, page_id, use_html=False
            )
            is_caption = trigger_message.caption is not None
            success = await self._safe_edit_message(
                trigger_message,
                description,
                notion_html,
                notion_plain,
                is_caption=is_caption,
            )
            if not success:
                try:
                    await trigger_message.reply_text(
                        f"✅ 노션 등록완료\n🔗 {page_url}"
                    )
                except Exception:
                    pass

            logger.info(
                f"매물 저장 완료: {property_data.get('주소', '?')}, "
                f"사진 {len(photo_urls)}장"
            )

            # ── 동일 주소 중복 감지 알림 (방법 A) ──
            address = property_data.get("주소", "")
            if address:
                duplicates = (
                    self.notion_uploader.find_pages_by_address(
                        address, exclude_page_id=page_id
                    )
                )
                if duplicates:
                    dup_msg = (
                        f"⚠️ 동일 주소 매물 감지!\n"
                        f"📍 {address}\n\n"
                        f"기존 등록된 매물:\n"
                    )
                    for dup in duplicates[:3]:
                        dup_msg += (
                            f"• {dup['title']}\n"
                            f"  🔗 {dup['url']}\n"
                        )
                    if len(duplicates) > 3:
                        dup_msg += f"... 외 {len(duplicates) - 3}개\n"
                    dup_msg += (
                        "\n💡 기존 매물 확인 후 "
                        "필요시 보관처리 해주세요."
                    )
                    try:
                        await trigger_message.reply_text(dup_msg)
                    except Exception:
                        pass

        except Exception as e:
            logger.error(f"매물 저장 오류: {e}", exc_info=True)
            try:
                await trigger_message.reply_text(f"❌ 오류 발생: {str(e)}")
            except Exception:
                pass

    # ──────────────────────────────────────────────
    # 사진 메시지 처리
    # ──────────────────────────────────────────────

    async def handle_photo_message(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ):
        """사진 메시지 처리 (그룹/채널 + 앨범/단일 사진)"""
        message = update.effective_message
        if not message:
            return

        # 동기화 중 전달된 메시지 무시
        if self._sync_in_progress and message.forward_origin:
            return

        media_group_id = message.media_group_id

        if media_group_id:
            # ── 앨범(여러 장) 사진 처리 ──
            await self._collect_media_group(message, context)
        else:
            # ── 단일 사진 처리 ──
            caption = message.caption

            # 사진 URL 가져오기
            try:
                photo = message.photo[-1]
                photo_file = await photo.get_file()
                photo_url = photo_file.file_path
            except Exception as e:
                logger.error(f"사진 URL 가져오기 실패: {e}")
                return

            # ── 답장인 경우 추가사진 여부 확인 ──
            if message.reply_to_message:
                handled = await self._handle_extra_photo_reply(
                    message, context, [photo_url], caption
                )
                if handled:
                    return
                # 추가사진이 아닌 답장 사진 → 무시
                return

            # 채팅 버퍼에 사진 추가
            self._add_photos_to_buffer(
                message.chat_id, [photo_url], message,
                message.author_signature,
            )

            # 캡션이 매물 형식이면 → 30초 후 저장 예약
            if caption and self._is_listing_format(caption):
                await self._schedule_property_save(
                    message.chat_id, caption, message, context
                )
            # 캡션 없거나 매물 형식 아니면 → 사진만 버퍼에 보관

    async def _collect_media_group(self, message, context):
        """앨범 사진을 수집하고, 타임아웃 후 일괄 처리"""
        media_group_id = message.media_group_id

        # 첫 번째 사진이면 그룹 초기화
        if media_group_id not in self._media_groups:
            self._media_groups[media_group_id] = {
                "photos": [],
                "caption": None,
                "message": message,
                "author_signature": message.author_signature,
                "context": context,  # 30초 저장 버퍼에서 사용
                "reply_to_message": message.reply_to_message,  # 답장 대상 메시지
            }

        # 사진 추가 (가장 큰 해상도)
        photo = message.photo[-1]
        photo_file = await photo.get_file()
        self._media_groups[media_group_id]["photos"].append(
            photo_file.file_path
        )

        # 캡션이 있으면 저장
        if message.caption:
            self._media_groups[media_group_id]["caption"] = (
                message.caption
            )
            self._media_groups[media_group_id]["message"] = message

        # 기존 타이머가 있으면 취소
        task_key = f"media_group_{media_group_id}"
        if task_key in self._pending_tasks:
            self._pending_tasks[task_key].cancel()

        # 새 타이머 설정 (2초 후 처리)
        self._pending_tasks[task_key] = asyncio.create_task(
            self._delayed_process_media_group(media_group_id)
        )

    async def _delayed_process_media_group(self, media_group_id):
        """일정 시간 대기 후 미디어 그룹 처리"""
        await asyncio.sleep(self.MEDIA_GROUP_TIMEOUT)
        await self._process_media_group(media_group_id)

    async def _process_media_group(self, media_group_id):
        """수집된 앨범 사진을 채팅 버퍼에 추가하고, 캡션이 매물 설명이면 저장 예약"""
        task_key = f"media_group_{media_group_id}"
        self._pending_tasks.pop(task_key, None)

        group_data = self._media_groups.pop(media_group_id, None)
        if not group_data:
            return

        message = group_data["message"]
        caption = group_data["caption"]
        photo_urls = group_data["photos"]
        context = group_data.get("context")
        author_sig = group_data.get("author_signature")
        reply_to = group_data.get("reply_to_message")
        chat_id = message.chat_id

        # ── 답장 앨범인 경우 추가사진 여부 확인 ──
        if reply_to and context:
            handled = await self._handle_extra_photo_reply(
                message, context, photo_urls, caption
            )
            if handled:
                return
            # 추가사진이 아닌 답장 앨범 → 무시 (일반 채팅 답장 등)
            return

        # 사진을 채팅 버퍼에 추가 (복수 미디어그룹 묶음 처리)
        self._add_photos_to_buffer(chat_id, photo_urls, message, author_sig)

        # 캡션이 매물 형식(1. 2. 3...)이면 → 30초 후 저장 예약
        if caption and self._is_listing_format(caption) and context:
            await self._schedule_property_save(
                chat_id, caption, message, context
            )
        # 캡션 없거나 매물 형식 아니면 → 사진만 버퍼에 보관, 텍스트 대기

    # ──────────────────────────────────────────────
    # 텔레그램 ↔ 노션 동기화 (삭제 감지)
    # ──────────────────────────────────────────────

    # 자동 동기화 주기 (초) = 4시간
    AUTO_SYNC_INTERVAL = 4 * 60 * 60

    @staticmethod
    async def _check_message_exists(
        bot, chat_id: int, message_id: int
    ) -> bool:
        """텔레그램 메시지 존재 여부를 비파괴적으로 확인

        edit_message_reply_markup 호출 결과로 판별:
        - 메시지 존재: 'not modified' 에러 → True
        - 메시지 삭제됨: 'not found' 에러 → False
        """
        try:
            await bot.edit_message_reply_markup(
                chat_id=chat_id,
                message_id=message_id,
            )
            # 성공 시 → 메시지 존재 (리플라이 마크업 변경됨)
            return True
        except Exception as e:
            err = str(e).lower()
            if "not found" in err or "message to edit" in err:
                return False
            if "not modified" in err:
                return True
            if "message can't be edited" in err:
                return True
            if "there is no reply_markup" in err:
                return True
            # 네트워크 에러 등 → 안전하게 존재한다고 가정
            logger.warning(
                f"메시지 존재 확인 불확실 "
                f"(chat={chat_id}, msg={message_id}): {e}"
            )
            return True

    async def _sync_deleted_properties(
        self, bot, report_chat_id: int = None
    ) -> Dict:
        """텔레그램에서 삭제된 매물을 노션에서 아카이브

        Args:
            bot: 텔레그램 봇 인스턴스
            report_chat_id: 결과를 보고할 채팅 ID (None이면 무음)

        Returns:
            {"checked": int, "archived": int,
             "archived_titles": List[str],
             "notion_count": int, "memory_count": int}
        """
        self._sync_in_progress = True
        result = {
            "checked": 0,
            "archived": 0,
            "archived_titles": [],
            "notion_count": 0,
            "memory_count": 0,
        }

        try:
            # ── 1단계: 노션 DB에서 추적 중인 페이지 조회 ──
            tracked_pages = (
                self.notion_uploader.get_tracked_pages()
            )
            result["notion_count"] = len(tracked_pages)
            notion_msg_ids = {
                p["msg_id"] for p in tracked_pages
            }

            # ── 2단계: 메모리 매핑도 추가 (중복 제거) ──
            for msg_id, page_id in list(
                self._page_mapping.items()
            ):
                if msg_id in notion_msg_ids:
                    continue  # 노션에 이미 있으면 스킵
                chat_id = self._msg_chat_ids.get(msg_id)
                if not chat_id and report_chat_id:
                    chat_id = report_chat_id
                if chat_id:
                    tracked_pages.append(
                        {
                            "page_id": page_id,
                            "chat_id": int(chat_id),
                            "msg_id": int(msg_id),
                            "title": "(메모리)",
                        }
                    )
                    result["memory_count"] += 1

            logger.info(
                f"동기화 시작: 총 {len(tracked_pages)}개 매물 "
                f"(노션 {result['notion_count']}개 + "
                f"메모리 {result['memory_count']}개)"
            )

            for page_info in tracked_pages:
                page_id = page_info["page_id"]
                chat_id = page_info["chat_id"]
                msg_id = page_info["msg_id"]
                title = page_info["title"] or "제목 없음"

                result["checked"] += 1

                # 메시지 존재 여부 확인
                exists = await self._check_message_exists(
                    bot, chat_id, msg_id
                )

                if not exists:
                    # 텔레그램에서 삭제됨 → 노션 아카이브
                    try:
                        self.notion_uploader.archive_property(
                            page_id
                        )
                        result["archived"] += 1
                        result["archived_titles"].append(title)

                        # 메모리 매핑도 정리
                        self._page_mapping.pop(msg_id, None)
                        self._original_texts.pop(msg_id, None)
                        self._msg_chat_ids.pop(msg_id, None)

                        logger.info(
                            f"동기화 삭제: '{title}' "
                            f"(msg_id={msg_id})"
                        )
                    except Exception as e:
                        logger.error(
                            f"동기화 아카이브 실패 "
                            f"'{title}': {e}"
                        )

                # API 속도 제한 방지 (0.5초 간격)
                await asyncio.sleep(0.5)

            logger.info(
                f"동기화 완료: {result['checked']}개 확인, "
                f"{result['archived']}개 삭제"
            )

        except Exception as e:
            logger.error(f"동기화 처리 오류: {e}", exc_info=True)
        finally:
            self._sync_in_progress = False

        return result

    async def sync_command(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ):
        """/동기화 명령어: 수동으로 텔레그램-노션 동기화 실행"""
        message = update.effective_message
        if not message:
            return

        logger.info(
            f"/동기화 명령어 수신 (chat_id={message.chat_id})"
        )

        mem_count = len(self._page_mapping)
        status_msg = await message.reply_text(
            "🔄 동기화 시작...\n"
            f"메모리 추적 매물: {mem_count}개\n"
            "노션 DB를 조회하고 텔레그램 메시지 존재 여부를 "
            "확인합니다.\n"
            "(매물 수에 따라 시간이 걸릴 수 있습니다)"
        )

        try:
            result = await self._sync_deleted_properties(
                context.bot,
                report_chat_id=message.chat_id,
            )

            # 결과 메시지 생성
            report = (
                f"✅ 동기화 완료!\n\n"
                f"📊 확인한 매물: {result['checked']}개\n"
                f"  • 노션 DB 추적: "
                f"{result['notion_count']}개\n"
                f"  • 메모리 추적: "
                f"{result['memory_count']}개\n"
                f"🗑️ 삭제(아카이브): "
                f"{result['archived']}개\n"
            )

            if result["archived_titles"]:
                report += "\n삭제된 매물:\n"
                for title in result["archived_titles"][:20]:
                    report += f"  • {title}\n"
                if len(result["archived_titles"]) > 20:
                    extra = (
                        len(result["archived_titles"]) - 20
                    )
                    report += f"  ... 외 {extra}개\n"

            if result["checked"] == 0:
                report += (
                    "\n⚠️ 추적 중인 매물이 없습니다.\n"
                    "이 코드 업데이트 이후 새로 등록된 "
                    "매물부터 동기화가 가능합니다."
                )
            elif result["archived"] == 0:
                report += (
                    "\n💡 텔레그램에서 삭제된 매물이 없습니다. "
                    "모든 매물이 정상입니다!"
                )

            await status_msg.edit_text(report)

        except Exception as e:
            logger.error(
                f"수동 동기화 오류: {e}", exc_info=True
            )
            await status_msg.edit_text(
                f"❌ 동기화 중 오류 발생: {str(e)}"
            )

    async def _post_init(self, application):
        """봇 초기화 후 자동 동기화 백그라운드 태스크 시작"""
        asyncio.create_task(
            self._auto_sync_loop(application)
        )
        logger.info(
            f"자동 동기화 태스크 시작 "
            f"(주기: {self.AUTO_SYNC_INTERVAL // 3600}시간)"
        )

    async def _auto_sync_loop(self, application):
        """백그라운드에서 주기적으로 동기화 실행"""
        # 봇 시작 후 2분 대기 (초기화 안정화)
        await asyncio.sleep(120)

        while True:
            try:
                logger.info("⏰ 자동 동기화 실행 중...")
                result = await self._sync_deleted_properties(
                    application.bot
                )
                if result["archived"] > 0:
                    logger.info(
                        f"⏰ 자동 동기화: "
                        f"{result['archived']}개 매물 삭제됨"
                    )
            except Exception as e:
                logger.error(
                    f"자동 동기화 오류: {e}", exc_info=True
                )

            # 다음 동기화까지 대기
            await asyncio.sleep(self.AUTO_SYNC_INTERVAL)

    # ──────────────────────────────────────────────
    # 텍스트 메시지 처리
    # ──────────────────────────────────────────────

    # ──────────────────────────────────────────────
    # 추가사진 기능 (답장으로 기존 노션 매물에 사진 추가)
    # ──────────────────────────────────────────────

    @staticmethod
    def _is_extra_photo_caption(caption: str) -> Tuple[bool, str]:
        """추가사진 캡션 감지 및 라벨 추출

        인식 패턴 (공백/순서 무관):
            추가사진, 추가 사진, 철거 추가사진,
            추가사진 철거, 추가 철거사진 등

        Returns:
            (is_extra, label)
            - is_extra: True면 추가사진 답장
            - label: "추가사진" 또는 "추가사진 (철거)" 등
        """
        if not caption:
            return False, ""
        # 공백 제거 후 키워드 체크
        normalized = re.sub(r"\s+", "", caption)
        if "추가" in normalized and "사진" in normalized:
            # '추가', '사진' 제거 후 남은 키워드 → 부가 라벨
            extra_kw = re.sub(r"[추가사진]", "", caption)
            extra_kw = re.sub(r"\s+", " ", extra_kw).strip()
            label = f"추가사진 ({extra_kw})" if extra_kw else "추가사진"
            return True, label
        return False, ""

    def _get_extra_photo_page_id(
        self, orig_msg_id: int
    ) -> Optional[str]:
        """원본 메시지 ID → 노션 페이지 ID 조회
        메모리 매핑 우선, 없으면 노션 DB 검색
        """
        if orig_msg_id in self._page_mapping:
            return self._page_mapping[orig_msg_id]
        return self.notion_uploader.find_page_by_msg_id(orig_msg_id)

    async def _handle_extra_photo_reply(
        self,
        message,
        context,
        photo_urls: List[str],
        caption: str = None,
    ) -> bool:
        """사진 답장 처리 → 추가사진이면 노션에 추가하고 True 반환"""
        reply = message.reply_to_message
        if not reply:
            return False

        orig_msg_id = reply.message_id
        cap = caption or message.caption or ""

        is_extra, extra_label = self._is_extra_photo_caption(cap)
        already_in_buffer = orig_msg_id in self._extra_photo_buffers

        if not (is_extra or already_in_buffer):
            # 추가사진 캡션도 없고 기존 버퍼도 없음 → 무시
            return False

        page_id = self._get_extra_photo_page_id(orig_msg_id)
        if not page_id:
            logger.debug(
                f"답장 대상 메시지({orig_msg_id})의 노션 페이지 없음 - 무시"
            )
            return False

        label = (
            extra_label
            if is_extra
            else self._extra_photo_buffers.get(
                orig_msg_id, {}
            ).get("label", "추가사진")
        )

        await self._schedule_extra_photo_save(
            orig_msg_id, photo_urls, label, page_id, context.bot
        )
        logger.info(
            f"추가사진 버퍼 추가: orig_msg={orig_msg_id}, "
            f"{len(photo_urls)}장, 라벨={label}"
        )
        return True

    async def _schedule_extra_photo_save(
        self,
        orig_msg_id: int,
        photos: List[str],
        label: str,
        page_id: str,
        bot,
    ):
        """추가사진 버퍼에 사진 추가 + 30초 타이머 리셋"""
        if orig_msg_id not in self._extra_photo_buffers:
            self._extra_photo_buffers[orig_msg_id] = {
                "photos": [],
                "label": label,
                "page_id": page_id,
                "timer_task": None,
            }

        buf = self._extra_photo_buffers[orig_msg_id]
        buf["photos"].extend(photos)
        if label:
            buf["label"] = label  # 새 라벨로 업데이트

        # 기존 타이머 취소 후 30초 재시작
        if buf.get("timer_task"):
            buf["timer_task"].cancel()
        buf["timer_task"] = asyncio.create_task(
            self._do_save_extra_photos(orig_msg_id, bot)
        )

    async def _do_save_extra_photos(
        self, orig_msg_id: int, bot
    ):
        """30초 대기 후 추가사진을 노션 페이지에 저장"""
        await asyncio.sleep(self.PROPERTY_SAVE_BUFFER)

        buf = self._extra_photo_buffers.pop(orig_msg_id, None)
        if not buf:
            return

        photos = buf.get("photos", [])
        label = buf.get("label", "추가사진")
        page_id = buf.get("page_id")

        if not photos or not page_id:
            return

        date_str = datetime.now().strftime("%y.%m.%d")
        full_label = f"{label} {date_str}"

        # 노션 블록: 구분선 + 헤딩 + 사진
        blocks: List[Dict] = [
            {"object": "block", "type": "divider", "divider": {}},
            {
                "object": "block",
                "type": "heading_2",
                "heading_2": {
                    "rich_text": [
                        {"text": {"content": f"📷 {full_label}"}}
                    ]
                },
            },
        ]
        blocks.extend(
            self.notion_uploader._build_photo_blocks(photos)
        )

        success = self.notion_uploader.append_blocks_to_page(
            page_id, blocks
        )
        if success:
            logger.info(
                f"추가사진 저장 완료: page_id={page_id}, "
                f"{len(photos)}장, 라벨={full_label}"
            )
        else:
            logger.error(f"추가사진 저장 실패: page_id={page_id}")

    async def handle_text_message(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ):
        """텍스트 전용 메시지 처리 (그룹/채널)"""
        message = update.effective_message
        if not message:
            return

        # 동기화 중 전달된 메시지 무시
        if self._sync_in_progress and message.forward_origin:
            return

        text = message.text or message.caption
        if not text:
            return

        # 텍스트 답장: 거래완료/계약완료 패턴만 처리, 나머지 무시
        # (예: "월세 조정됐습니다" 같은 알림성 답장은 노션에 저장하지 않음)
        if message.reply_to_message:
            is_deal, agent = self._parse_deal_complete(text)
            if is_deal:
                await self._handle_deal_complete_reply(
                    message, context, agent
                )
            return

        # ── 매물 설명인지 확인 (1. 2. 3... 번호 형식) ──
        if self._is_listing_format(text):
            # 채팅 버퍼의 사진들과 함께 30초 후 저장 예약
            await self._schedule_property_save(
                message.chat_id, text, message, context
            )
            return

        # ── 층수 라벨인지 확인 (30자 이하 짧은 텍스트) ──
        # 채팅 버퍼에 사진이 있을 때만 층수 라벨로 처리
        text_stripped = text.strip()
        if (
            len(text_stripped) <= 30
            and not text_stripped.startswith("/")
        ):
            # 층수 패턴 감지: "1층", "2층", "B1층", "지하층", "1,2층" 등
            floor_match = re.search(
                r'([B지하]?\d*(?:[,~\-]\d+)*층)', text_stripped
            )

            if message.chat_id in self._chat_buffers:
                if floor_match:
                    # 층수 라벨 → 버퍼에 라벨 추가 (사진 그룹 구분)
                    floor_label = floor_match.group(1)
                    self._add_floor_label_to_buffer(
                        message.chat_id, floor_label
                    )
                    logger.debug(
                        f"층수 라벨 인식: '{floor_label}' "
                        f"(chat_id={message.chat_id})"
                    )
                else:
                    # 층수 패턴은 없지만 짧은 텍스트 → 타이머 리셋만
                    logger.debug(
                        f"짧은 텍스트 (층수아님): '{text_stripped}'"
                    )

                # 버퍼 만료 타이머 리셋 (2분 연장)
                existing = self._collect_tasks.get(message.chat_id)
                if existing:
                    existing.cancel()
                self._collect_tasks[message.chat_id] = asyncio.create_task(
                    self._expire_chat_buffer(message.chat_id)
                )

    # ──────────────────────────────────────────────
    # 봇 실행
    # ──────────────────────────────────────────────

    def run(self):
        """봇 실행"""
        if sys.version_info >= (3, 10):
            try:
                asyncio.get_running_loop()
            except RuntimeError:
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)

        application = (
            Application.builder()
            .token(self.telegram_token)
            .post_init(self._post_init)
            .build()
        )

        # 명령어 핸들러 (일반 메시지)
        application.add_handler(
            CommandHandler("start", self.start_command)
        )
        application.add_handler(
            CommandHandler("help", self.help_command)
        )
        application.add_handler(
            CommandHandler("check", self.check_command)
        )
        application.add_handler(
            CommandHandler("delete", self.delete_command)
        )
        # 한글 명령어는 Regex로 처리
        application.add_handler(
            MessageHandler(
                filters.Regex(r"^/동기화")
                & (
                    filters.UpdateType.MESSAGE
                    | filters.UpdateType.CHANNEL_POST
                ),
                self.sync_command,
            )
        )
        application.add_handler(
            MessageHandler(
                filters.Regex(r"^/매물확인")
                & (
                    filters.UpdateType.MESSAGE
                    | filters.UpdateType.CHANNEL_POST
                ),
                self.property_check_command,
            )
        )

        # 명령어 핸들러 (채널 포스트)
        application.add_handler(
            MessageHandler(
                filters.Regex(r"^/start")
                & filters.UpdateType.CHANNEL_POST,
                self.start_command,
            )
        )
        application.add_handler(
            MessageHandler(
                filters.Regex(r"^/help")
                & filters.UpdateType.CHANNEL_POST,
                self.help_command,
            )
        )
        application.add_handler(
            MessageHandler(
                filters.Regex(r"^/check")
                & filters.UpdateType.CHANNEL_POST,
                self.check_command,
            )
        )
        application.add_handler(
            MessageHandler(
                filters.Regex(r"^/delete")
                & filters.UpdateType.CHANNEL_POST,
                self.delete_command,
            )
        )
        application.add_handler(
            MessageHandler(
                filters.Regex(r"^/매물확인")
                & filters.UpdateType.CHANNEL_POST,
                self.property_check_command,
            )
        )

        # 채널/그룹 메시지 수정 감지
        # 별도 그룹(group=1)에 등록하여 기존 핸들러와 독립적으로 동작
        # UpdateFilter만 사용하면 MessageHandler 내부 2차 필터링에서 실패하므로
        # filters.ALL을 사용하고 콜백에서 수정 여부를 직접 확인
        application.add_handler(
            MessageHandler(
                filters.ALL,
                self.handle_edited_message,
            ),
            group=1,
        )

        # 사진 메시지 (그룹 + 채널)
        application.add_handler(
            MessageHandler(
                filters.PHOTO
                & (
                    filters.UpdateType.MESSAGE
                    | filters.UpdateType.CHANNEL_POST
                ),
                self.handle_photo_message,
            )
        )

        # 텍스트 전용 메시지 (그룹 + 채널, 명령어 제외)
        application.add_handler(
            MessageHandler(
                filters.TEXT
                & ~filters.COMMAND
                & (
                    filters.UpdateType.MESSAGE
                    | filters.UpdateType.CHANNEL_POST
                ),
                self.handle_text_message,
            )
        )

        logger.info("봇이 시작되었습니다...")
        try:
            print("🤖 봇이 시작되었습니다...")
            print(
                "텔레그램에서 사진과 매물 정보를 전송하면 "
                "자동으로 노션에 등록됩니다."
            )
            print("📷 여러 장 사진 앨범도 지원됩니다!")
            print(
                "✏️ 원본 메시지를 수정하면 "
                "노션에도 자동으로 반영됩니다!"
            )
            print(
                "🗑️ 매물 메시지에 답장으로 /delete → "
                "노션+텔레그램 모두 삭제!"
            )
            print(
                "🔄 4시간마다 자동 동기화 "
                "(삭제된 매물 노션에서 정리)"
            )
            print(
                "/동기화 명령어로 수동 동기화를 "
                "실행할 수 있습니다."
            )
        except UnicodeEncodeError:
            print("[BOT] 봇이 시작되었습니다...")
            print(
                "텔레그램에서 사진과 매물 정보를 전송하면 "
                "자동으로 노션에 등록됩니다."
            )

        application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
    NOTION_TOKEN = os.getenv("NOTION_TOKEN")
    DATABASE_ID = os.getenv("NOTION_DATABASE_ID")

    if not all([TELEGRAM_TOKEN, NOTION_TOKEN, DATABASE_ID]):
        print("=" * 50)
        print("환경변수를 설정해주세요!")
        print("=" * 50)
        print()
        missing = []
        if not TELEGRAM_TOKEN:
            missing.append("TELEGRAM_BOT_TOKEN")
        if not NOTION_TOKEN:
            missing.append("NOTION_TOKEN")
        if not DATABASE_ID:
            missing.append("NOTION_DATABASE_ID")
        print(f"누락된 변수: {', '.join(missing)}")
        exit(1)

    bot = TelegramNotionBot(
        TELEGRAM_TOKEN, NOTION_TOKEN, DATABASE_ID
    )
    bot.run()
