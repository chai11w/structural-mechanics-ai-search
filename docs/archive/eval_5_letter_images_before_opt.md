# 5 张字母荷载图片 LLM 识别评测

## A

- image: `2静定结构\2多跨梁\内力图\题目2\2跨\1力\39.jpg`
- expected: `{"loads": [{"type": "集中", "raw": "ql"}]}`
- actual: `{"loads": [{"type": "集中", "raw": "ql"}, {"type": "均布", "raw": "q"}]}`

## B

- image: `2静定结构\2多跨梁\内力图\题目2\3跨\1力\34.jpg`
- expected: `{"loads": [{"type": "集中", "raw": "2P"}, {"type": "集中", "raw": "4P"}, {"type": "弯矩", "raw": "Pa"}]}`
- actual: `{"loads": [{"type": "集中", "raw": "2P"}, {"type": "集中", "raw": "4P"}, {"type": "均布", "raw": "2q"}, {"type": "弯矩", "raw": "Pa"}]}`

## C

- image: `2静定结构\2多跨梁\内力图\题目2\44.jpg`
- expected: `{"loads": [{"type": "弯矩", "raw": "m"}, {"type": "弯矩", "raw": "m"}, {"type": "弯矩", "raw": "m"}, {"type": "弯矩", "raw": "m"}, {"type": "弯矩", "raw": "m"}]}`
- actual: `{"loads": [{"type": "弯矩", "raw": "m"}, {"type": "弯矩", "raw": "m"}, {"type": "弯矩", "raw": "m"}, {"type": "弯矩", "raw": "m"}, {"type": "弯矩", "raw": "m"}, {"type": "弯矩", "raw": "m"}]}`

## D

- image: `2静定结构\3钢架\1内力图\题目2\3铰\50.jpg`
- expected: `{"loads": [{"type": "均布", "raw": "q"}]}`
- actual: `{"loads": [{"type": "均布", "raw": "q"}]}`

## E

- image: `2静定结构\3钢架\2弯矩图\题目a\3铰\14.jpg`
- expected: `{"loads": [{"type": "弯矩", "raw": "qa²"}]}`
- actual: `{"loads": [{"type": "均布", "raw": "q"}, {"type": "弯矩", "raw": "qa²"}]}`
