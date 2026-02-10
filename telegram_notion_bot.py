#!/usr/bin/env python3
"""
텔레그램 부동산 매물 -> 노션 자동 등록 봇
(여러 장 사진 앨범 지원 + 답장으로 매물 수정)
"""

import os
import re
import sys
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

        lines = text.strip().split("\n")
        data = {}

        start_idx = 0
        if not skip_address and lines:
            data["주소"] = lines[0].strip()
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

            # 4. 건축물용도 / 면적
            elif line.startswith("4."):
                content4 = re.sub(r"^4\.\s*", "", line).strip()

                계약_match = re.search(
                    r"계약(?:면적)?\s*(\d+\.?\d*)\s*(?:m2|㎡)",
                    content4,
                )
                if 계약_match:
                    data["계약면적"] = float(계약_match.group(1))

                전용_match = re.search(
                    r"전용(?:면적)?\s*(\d+\.?\d*)\s*(?:m2|㎡)",
                    content4,
                )
                if 전용_match:
                    data["전용면적"] = float(전용_match.group(1))

                # 건축물용도: "계약(면적)" 또는 "전용(면적)" 앞의 텍스트 추출
                용도_text = re.split(
                    r'\s*/\s*계약(?:면적)?|\s+계약(?:면적)?'
                    r'|\s*/\s*전용(?:면적)?|\s+전용(?:면적)?',
                    content4,
                )[0].strip().rstrip(' /')
                if 용도_text:
                    data["건축물용도"] = (
                        PropertyParser._normalize_building_use(용도_text)
                    )

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
                if parking_text:
                    if re.search(
                        r'주차\s*[xX]|주차\s*불가|주차\s*안\s*됨',
                        parking_text,
                    ):
                        data["주차"] = "불가능"
                    else:
                        data["주차"] = "가능"
                        # 주차 메모 추출
                        pmemo = re.sub(
                            r'^주차\s*[는은]?\s*', '', parking_text
                        ).strip()
                        pmemo = re.sub(r'^[oO]\s*', '', pmemo).strip()
                        pmemo = re.sub(r'^장\s*사용', '주차장', pmemo)
                        pmemo = pmemo.replace('(', ' ').replace(')', '')
                        pmemo = re.sub(r'가능\S*', '', pmemo).strip()
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
        """건축물용도 약어를 정식 명칭으로 정규화"""
        text = text.strip()
        if re.search(r'(제\s*)?1\s*종', text):
            return "제1종근린생활시설"
        if re.search(r'(제\s*)?2\s*종', text):
            return "제2종근린생활시설"
        return text


class NotionUploader:
    """노션 업로드 클래스"""

    def __init__(self, notion_token: str, database_id: str):
        self.client = Client(auth=notion_token)
        self.database_id = database_id

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
        층_match = re.search(r"(\d+)층", 주소)
        if 층_match:
            properties["층수"] = {
                "multi_select": [{"name": f"{층_match.group(1)}층"}]
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

        # ── 🏢건축물용도 (select) ──
        if "건축물용도" in property_data:
            properties["🏢건축물용도"] = {
                "select": {
                    "name": property_data["건축물용도"][:100]
                }
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

        # ── 거래 상태 (select) - 신규 등록 시에만 ──
        if not is_update:
            properties["거래 상태"] = {
                "select": {"name": "거래 가능"}
            }

        return properties

    def upload_property(
        self,
        property_data: Dict,
        photo_urls: Optional[List[str]] = None,
    ) -> Tuple[str, str]:
        """
        노션 데이터베이스에 매물 등록 (여러 장 사진 지원)

        Returns:
            (page_url, page_id) 튜플
        """
        properties = self._build_notion_properties(property_data)

        # ──────────────────────────────────────────────
        # 페이지 내용 (본문 블록) - 여러 장 사진 지원
        # ──────────────────────────────────────────────
        children = []

        # 모든 사진 추가 (2열 컬럼 레이아웃)
        if photo_urls:
            for i in range(0, len(photo_urls), 2):
                pair = photo_urls[i : i + 2]
                if len(pair) == 2:
                    # 2장을 나란히 배치
                    children.append(
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
                                                            "url": pair[
                                                                0
                                                            ]
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
                                                            "url": pair[
                                                                1
                                                            ]
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
                    children.append(
                        {
                            "object": "block",
                            "type": "image",
                            "image": {
                                "type": "external",
                                "external": {"url": pair[0]},
                            },
                        }
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

    def get_page_properties(self, page_id: str) -> Dict:
        """노션 페이지의 현재 속성값을 파싱하여 반환"""
        try:
            page = self.client.pages.retrieve(page_id=page_id)
            props = page.get("properties", {})
            result = {}

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
                ("건축물용도", "🏢건축물용도"),
                ("주차", "🅿️주차"),
                ("방향", "📍방향"),
                ("화장실 위치", "🚻화장실 위치"),
                ("화장실 수", "🚻화장실 수"),
                ("위반건축물", "🚨위반건축물"),
            ]:
                if notion_key in props:
                    sel = props[notion_key].get("select")
                    if sel:
                        result[key] = sel.get("name", "")

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


class TelegramNotionBot:
    """텔레그램-노션 연동 봇 (앨범/여러 장 사진 + 답장 수정 지원)"""

    # 앨범 사진 수집 대기 시간 (초)
    MEDIA_GROUP_TIMEOUT = 2.0

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
        "• ✅ 등록 메시지에 *답장* → 매물 정보 수정\n\n"
        "📌 *수정 방법:*\n"
        "봇의 ✅ 등록 메시지에 답장으로 수정할 항목만 보내세요\n"
        "예: `1\\.3000/150 부별` → 보증금/월세/부가세만 수정\n\n"
        "📌 *명령어:*\n"
        "/start \\- 봇 시작\n"
        "/help \\- 도움말 보기"
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
        # 메시지 ID → 노션 페이지 ID 매핑 (원본 + ✅ 메시지 모두)
        self._page_mapping: Dict[int, str] = {}
        # 페이지 ID → ✅ 확인 메시지 정보 (수정 시 ✅ 메시지 찾기용)
        self._confirm_msg_info: Dict[str, Dict] = {}

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
    # ✅ 등록/수정 확인 메시지 생성 헬퍼
    # ──────────────────────────────────────────────

    @staticmethod
    def _build_confirm_text(
        property_data: Dict, page_url: str, photo_count: int
    ) -> str:
        """✅ 등록 확인 메시지 텍스트 생성 (짧은 버전)"""
        return f"✅ 노션 등록완료\n🔗 {page_url}"

    # ──────────────────────────────────────────────
    # 답장(Reply) 기반 매물 수정 기능
    # ──────────────────────────────────────────────

    def _get_page_id_from_reply(
        self, reply_message
    ) -> Optional[str]:
        """답장 대상 메시지에서 노션 페이지 ID 추출
        (✅ 메시지 또는 원본 매물 게시물 모두 지원)
        """
        msg_id = reply_message.message_id

        # 1. 저장된 매핑에서 찾기 (원본 게시물 / ✅ 메시지 모두)
        if msg_id in self._page_mapping:
            return self._page_mapping[msg_id]

        # 2. 텍스트에서 Notion URL 추출 (봇 재시작 후 매핑 없을 때)
        text = reply_message.text or ""
        if "notion.so" in text:
            match = re.search(r'([a-f0-9]{32})', text)
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

        예: "  💎권리금: 4000 → 3000"
        → {"💎권리금": "4000 → 3000"}
        """
        result = {}
        for line in section_text.split("\n"):
            line = line.strip()
            if not line or line.startswith("📝"):
                continue
            match = re.match(r'(.+?):\s*(.+)', line)
            if match:
                result[match.group(1).strip()] = (
                    match.group(2).strip()
                )
        return result

    def _build_updated_confirm_text(
        self, old_text: str,
        변경_dict: Dict[str, str],
        now: str, page_url: str,
    ) -> str:
        """기존 ✅ 메시지에 수정 내역 반영
        (체인 이력 지원 + 이전 수정, 최대 2건)

        Args:
            변경_dict: {필드라벨: "old → new"} 형태
        """
        # ── 🔗 링크 파트 분리 ──
        if "🔗" in old_text:
            link_idx = old_text.index("🔗")
            link_part = old_text[link_idx:]
        else:
            link_part = (
                f"🔗 {page_url}\n\n"
                f"💡 이 메시지에 답장하면 매물 수정\n"
                f"   특이사항 🔄 전체교체\n"
                f"   특이사항+ ➕ 기존내용에 이어쓰기"
            )
            link_idx = len(old_text)

        # ── 기존 수정 이력 파싱 ──
        old_최근 = {}
        old_최근_time = ""
        old_이전 = {}

        if "📝 최근 수정" in old_text:
            최근_시작 = old_text.index("📝 최근 수정")
            최근_끝 = link_idx
            for boundary in ["┈", "📝 이전 수정"]:
                try:
                    b_idx = old_text.index(
                        boundary, 최근_시작 + 1
                    )
                    if b_idx < 최근_끝:
                        최근_끝 = b_idx
                except ValueError:
                    pass

            최근_text = old_text[최근_시작:최근_끝].strip()
            time_match = re.search(r'\((.+?)\)', 최근_text)
            if time_match:
                old_최근_time = time_match.group(1)
            old_최근 = self._parse_change_section(최근_text)
            base_part = old_text[:최근_시작].rstrip()
        else:
            base_part = old_text[:link_idx].rstrip()

        if "📝 이전 수정" in old_text:
            이전_시작 = old_text.index("📝 이전 수정")
            이전_text = old_text[이전_시작:link_idx].strip()
            old_이전 = self._parse_change_section(이전_text)

        # ── 체인 병합 ──
        merged = {}
        for field, new_chain in 변경_dict.items():
            if field in old_최근:
                # 기존 체인에 새 값 추가
                old_chain = old_최근[field]
                new_end = new_chain.split("→")[-1].strip()
                merged[field] = f"{old_chain} → {new_end}"
            elif field in old_이전:
                old_chain = old_이전[field]
                new_end = new_chain.split("→")[-1].strip()
                merged[field] = f"{old_chain} → {new_end}"
            else:
                merged[field] = new_chain

        # ── 최근 수정 빌드 (한 줄로) ──
        최근_items_str = ", ".join(
            [f"{f} {c}" for f, c in merged.items()]
        )
        수정_섹션 = f"📝 수정 ({now}): {최근_items_str}"

        # ── 이전 수정: old 최근 중 이번에 안 건드린 항목 (한 줄로) ──
        이전_items = {
            f: c
            for f, c in old_최근.items()
            if f not in 변경_dict
        }
        if 이전_items and old_최근_time:
            이전_items_str = ", ".join(
                [f"{f}" for f in 이전_items.keys()]
            )
            수정_섹션 += f"\n📝 이전 ({old_최근_time}): {이전_items_str}"

        return f"{base_part}\n\n{수정_섹션}\n\n{link_part}"

    async def _handle_update(
        self, message, page_id: str, context
    ):
        """답장 메시지로 노션 매물 정보 수정 (기존 ✅ 메시지 수정)"""
        text = message.caption or message.text
        reply_msg = message.reply_to_message

        if not text:
            await message.reply_text(
                "❌ 수정할 내용이 없습니다.\n"
                "수정할 항목을 텍스트로 보내주세요."
            )
            return

        try:
            # 수정 모드로 파싱 (첫 줄도 데이터로 처리)
            property_data = self.parser.parse_property_info(
                text, skip_address=True
            )

            if not property_data:
                await message.reply_text(
                    "❌ 수정할 내용을 인식하지 못했습니다."
                )
                return

            loading_msg = await message.reply_text(
                "⏳ 노션 매물 정보 수정 중..."
            )

            # ── 수정 전 기존 값 조회 ──
            old_data = (
                self.notion_uploader.get_page_properties(page_id)
            )

            # ── 특이사항 추가(+) 모드 처리 ──
            특이사항_is_append = property_data.pop(
                "특이사항_추가", False
            )
            if 특이사항_is_append and "특이사항" in property_data:
                old_special = old_data.get("특이사항", "")
                if old_special:
                    property_data["특이사항"] = (
                        old_special + "\n"
                        + property_data["특이사항"]
                    )

            page_url = self.notion_uploader.update_property(
                page_id, property_data
            )

            # ── 변경 전→후 비교 (변경_dict 생성) ──
            field_names = {
                "보증금": "💰보증금",
                "월세": "💰월세",
                "부가세": "🧾부가세",
                "관리비": "⚡관리비",
                "권리금": "💎권리금",
                "건축물용도": "🏢건축물용도",
                "계약면적": "📐계약면적",
                "전용면적": "📐전용면적",
                "주차": "🅿️주차",
                "방향": "📍방향",
                "화장실 위치": "🚻화장실 위치",
                "화장실 수": "🚻화장실 수",
                "위반건축물": "🚨위반건축물",
                "대표 연락처": "📞연락처",
                "특이사항": "📢특이사항",
            }
            변경_dict = {}
            for key, label in field_names.items():
                if key not in property_data:
                    continue
                new_val = property_data[key]
                old_val = old_data.get(key)

                # 특이사항은 긴 텍스트 → 간단하게 표시
                if key == "특이사항":
                    if str(old_val or "") != str(new_val):
                        if 특이사항_is_append:
                            변경_dict[label] = "추가됨"
                        else:
                            변경_dict[label] = "수정됨"
                    continue

                # 숫자 비교 (float→int 변환)
                if (
                    isinstance(old_val, (int, float))
                    and isinstance(new_val, (int, float))
                ):
                    if old_val != new_val:
                        old_disp = (
                            int(old_val)
                            if isinstance(old_val, float)
                            and old_val == int(old_val)
                            else old_val
                        )
                        변경_dict[label] = (
                            f"{old_disp} → {new_val}"
                        )
                elif old_val is not None:
                    if str(old_val) != str(new_val):
                        변경_dict[label] = (
                            f"{old_val} → {new_val}"
                        )
                else:
                    # 기존에 없던 값이 새로 추가
                    변경_dict[label] = str(new_val)

            if not 변경_dict:
                변경_dict["📋내용"] = "수정됨"

            now = datetime.now().strftime("%m/%d")

            # ── 기존 ✅ 메시지를 찾아서 수정 ──
            edited_ok = False

            # 방법 1: reply_msg 가 ✅ 메시지인 경우 (직접 수정)
            if (
                reply_msg
                and reply_msg.text
                and "✅" in reply_msg.text
            ):
                try:
                    new_text = self._build_updated_confirm_text(
                        reply_msg.text, 변경_dict,
                        now, page_url,
                    )
                    await reply_msg.edit_text(new_text)
                    if page_id in self._confirm_msg_info:
                        self._confirm_msg_info[page_id][
                            "text"
                        ] = new_text
                    edited_ok = True
                except Exception as e:
                    logger.warning(
                        f"✅ 메시지 직접 수정 실패: {e}"
                    )

            # 방법 2: 원본 게시물에 답장한 경우 → 저장된 ✅ 메시지 수정
            if not edited_ok and page_id in self._confirm_msg_info:
                info = self._confirm_msg_info[page_id]
                try:
                    new_text = self._build_updated_confirm_text(
                        info["text"], 변경_dict,
                        now, page_url,
                    )
                    await context.bot.edit_message_text(
                        chat_id=info["chat_id"],
                        message_id=info["message_id"],
                        text=new_text,
                    )
                    info["text"] = new_text
                    edited_ok = True
                except Exception as e:
                    logger.warning(
                        f"✅ 메시지 간접 수정 실패: {e}"
                    )

            # 방법 3: ✅ 메시지를 찾을 수 없으면 새 메시지 전송
            if not edited_ok:
                변경_items_str = ", ".join(
                    [f"{k} {v}" for k, v in 변경_dict.items()]
                )
                await message.reply_text(
                    f"✅ 노션 등록완료\n"
                    f"🔗 {page_url}\n\n"
                    f"📝 수정 ({now}): {변경_items_str}"
                )

            # ── 중간 메시지 삭제 ──
            try:
                await loading_msg.delete()
            except Exception:
                pass

            # ── 수정 요청 메시지 삭제 (채널 깔끔 유지) ──
            try:
                await message.delete()
            except Exception:
                pass

        except Exception as e:
            logger.error(f"매물 수정 오류: {e}", exc_info=True)
            await message.reply_text(f"❌ 수정 오류: {str(e)}")

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

    # ──────────────────────────────────────────────
    # 사진 메시지 처리
    # ──────────────────────────────────────────────

    async def handle_photo_message(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ):
        """사진 메시지 처리 (그룹/채널 + 앨범/단일 사진 + 답장 수정)"""
        message = update.effective_message
        if not message:
            return

        # 답장(Reply)인 경우 → 수정 처리
        if message.reply_to_message:
            page_id = self._get_page_id_from_reply(
                message.reply_to_message
            )
            if page_id:
                # 매물 형식(1.~8.)이 아닌 답장은 무시 (사적 대화)
                reply_text = message.caption or message.text
                if not self._is_listing_format(
                    reply_text, is_update=True
                ):
                    return
                await self._handle_update(
                    message, page_id, context
                )
                return

        media_group_id = message.media_group_id

        if media_group_id:
            # ── 앨범(여러 장) 사진 처리 ──
            await self._collect_media_group(message, context)
        else:
            # ── 단일 사진 처리 ──
            caption = message.caption

            # 캡션이 없거나 매물 형식(1. 2. 3...)이 아니면 무시
            if not self._is_listing_format(caption):
                return

            try:
                property_data = self.parser.parse_property_info(
                    caption
                )
                property_data["원본 메시지"] = caption

                photo = message.photo[-1]
                photo_file = await photo.get_file()
                photo_url = photo_file.file_path

                loading_msg = await message.reply_text(
                    "⏳ 노션에 등록 중..."
                )
                page_url, page_id = (
                    self.notion_uploader.upload_property(
                        property_data, [photo_url]
                    )
                )

                confirm_text = self._build_confirm_text(
                    property_data, page_url, 1
                )
                confirm_msg = await message.reply_text(
                    confirm_text
                )

                # 매핑 저장 (✅ 메시지 + 원본 게시물)
                self._page_mapping[
                    confirm_msg.message_id
                ] = page_id
                self._page_mapping[
                    message.message_id
                ] = page_id
                self._confirm_msg_info[page_id] = {
                    "chat_id": confirm_msg.chat_id,
                    "message_id": confirm_msg.message_id,
                    "text": confirm_text,
                }

                # ⏳ 중간 메시지 삭제
                try:
                    await loading_msg.delete()
                except Exception:
                    pass

            except Exception as e:
                logger.error(
                    f"단일 사진 처리 오류: {e}", exc_info=True
                )
                await message.reply_text(
                    f"❌ 오류 발생: {str(e)}"
                )

    async def _collect_media_group(self, message, context):
        """앨범 사진을 수집하고, 타임아웃 후 일괄 처리"""
        media_group_id = message.media_group_id

        # 첫 번째 사진이면 그룹 초기화
        if media_group_id not in self._media_groups:
            self._media_groups[media_group_id] = {
                "photos": [],
                "caption": None,
                "message": message,
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
        """수집된 앨범 사진을 일괄 처리하여 노션에 업로드"""
        task_key = f"media_group_{media_group_id}"
        self._pending_tasks.pop(task_key, None)

        group_data = self._media_groups.pop(media_group_id, None)

        if not group_data:
            return

        message = group_data["message"]
        caption = group_data["caption"]
        photo_urls = group_data["photos"]

        # 캡션이 없거나 매물 형식(1. 2. 3...)이 아니면 무시
        if not self._is_listing_format(caption):
            return

        try:
            property_data = self.parser.parse_property_info(caption)
            property_data["원본 메시지"] = caption

            loading_msg = await message.reply_text(
                f"⏳ 노션에 등록 중... (사진 {len(photo_urls)}장)"
            )
            page_url, page_id = (
                self.notion_uploader.upload_property(
                    property_data, photo_urls
                )
            )

            confirm_text = self._build_confirm_text(
                property_data, page_url, len(photo_urls)
            )
            confirm_msg = await message.reply_text(
                confirm_text
            )

            # 매핑 저장 (✅ 메시지 + 원본 게시물)
            self._page_mapping[
                confirm_msg.message_id
            ] = page_id
            self._page_mapping[
                message.message_id
            ] = page_id
            self._confirm_msg_info[page_id] = {
                "chat_id": confirm_msg.chat_id,
                "message_id": confirm_msg.message_id,
                "text": confirm_text,
            }

            # ⏳ 중간 메시지 삭제
            try:
                await loading_msg.delete()
            except Exception:
                pass

        except Exception as e:
            logger.error(f"앨범 처리 오류: {e}", exc_info=True)
            await message.reply_text(f"❌ 오류 발생: {str(e)}")

    # ──────────────────────────────────────────────
    # 텍스트 메시지 처리
    # ──────────────────────────────────────────────

    async def handle_text_message(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ):
        """텍스트 전용 메시지 처리 (그룹/채널 + 답장 수정)"""
        message = update.effective_message
        if not message:
            return
        text = message.text or message.caption

        # 답장(Reply)인 경우 → 수정 처리
        if message.reply_to_message:
            page_id = self._get_page_id_from_reply(
                message.reply_to_message
            )
            if page_id:
                # 매물 형식(1.~8.)이 아닌 답장은 무시 (사적 대화)
                if not self._is_listing_format(
                    text, is_update=True
                ):
                    return
                await self._handle_update(
                    message, page_id, context
                )
                return

        # 매물 형식(1. 2. 3...)이 아니면 조용히 무시
        if not self._is_listing_format(text):
            return

        try:
            property_data = self.parser.parse_property_info(text)
            property_data["원본 메시지"] = text

            loading_msg = await message.reply_text(
                "⏳ 노션에 등록 중..."
            )
            page_url, page_id = (
                self.notion_uploader.upload_property(property_data)
            )

            confirm_text = self._build_confirm_text(
                property_data, page_url, 0
            )
            confirm_msg = await message.reply_text(
                confirm_text
            )

            # 매핑 저장 (✅ 메시지 + 원본 게시물)
            self._page_mapping[
                confirm_msg.message_id
            ] = page_id
            self._page_mapping[
                message.message_id
            ] = page_id
            self._confirm_msg_info[page_id] = {
                "chat_id": confirm_msg.chat_id,
                "message_id": confirm_msg.message_id,
                "text": confirm_text,
            }

            # ⏳ 중간 메시지 삭제
            try:
                await loading_msg.delete()
            except Exception:
                pass

        except Exception as e:
            logger.error(
                f"텍스트 메시지 처리 오류: {e}", exc_info=True
            )
            await message.reply_text(f"❌ 오류 발생: {str(e)}")

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
            .build()
        )

        # 명령어 핸들러
        application.add_handler(
            CommandHandler("start", self.start_command)
        )
        application.add_handler(
            CommandHandler("help", self.help_command)
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
                "✏️ 등록 확인 메시지에 답장하면 "
                "매물 정보를 수정할 수 있습니다!"
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
