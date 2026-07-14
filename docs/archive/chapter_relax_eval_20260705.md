# Chapter Relaxation Evaluation 2026-07-05

Purpose: evaluate the relaxed auto-chapter barrier with real live-bank images.

Settings:

- Qwen cache disabled.
- Sample: first 3 existing image rows from each supported live chapter workbook, total 21 images.
- Expected chapter: workbook chapter.
- `MANUAL` means no auto chapter was accepted; this is allowed for pure structure diagrams.

Final result after tightening false positives:

```text
auto_correct=13
auto_wrong=0
manual=8
total=21
```

Interpretation:

- The relaxed rules reduced manual/unknown cases for clear task-text images.
- Pure structure diagrams remained manual, as intended.
- A false positive was found during tuning: one `8影响线` sample was initially accepted as `2静定结构` because the model inferred “static beam inner force” from structure/task wording. The guard was tightened so inferred static-beam wording without quoted task/method evidence does not auto-pass as chapter 2.
- Another false positive was found during tuning: one `5位移法` sample was initially accepted as `6力矩分配` because the model inferred chapter from figure number `图6.2`. The guard was tightened so figure numbers/textbook chapter guesses do not auto-pass chapter 6.

Representative final outcomes:

| Expected | Effective | Status | Example |
|---|---|---|---|
| `2静定结构` | `2静定结构` | OK | `2静定结构/1单跨梁/题目a/1力/16.jpg` |
| `3静定结构位移` | `3静定结构位移` | OK | `3静定结构位移/1梁/题目a/1跨/12.jpg` |
| `4力法` | `4力法` | OK | `4力法/2钢架/1单未知量/题目1/1L/1提横/2固+饺/19.jpg` |
| `5位移法` | `5位移法` | OK | `5位移法/1梁/1单未知量/题目/1.jpg` |
| `6力矩分配` | `6力矩分配` | OK | `6力矩分配/1单节点分配/题目aa/1梁/11.jpg` |
| `7矩阵位移` | `7矩阵位移` | OK | `7矩阵位移/3全部步骤/题目aa/12.jpg` |
| `8影响线` | `8影响线` | OK | `8影响线/2数值计算/题目a/2.jpg` |

Manual examples that should remain manual:

- `2静定结构/1单跨梁/题目a/1力/13.jpg`: only structure/load/dimensions, no task text.
- `4力法/1梁/1单未知量/题目/2跨/11.jpg`: only structure/load/supports/dimensions, no method text.
- `5位移法/1梁/1单未知量/题目/4.jpg`: model inferred chapter 6 from figure number, correctly rejected after tightening.
- `8影响线/2数值计算/题目a/1跨/6.jpg`: model inferred static inner-force task, correctly rejected after tightening because it lacks direct chapter/method evidence.

