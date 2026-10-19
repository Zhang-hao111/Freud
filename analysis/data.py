"""Validated iteration-one inputs and leakage-safe MovieLens features."""

import hashlib
import json
import math
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import numpy as np


TABLES = ("users", "movies", "ratings")
GENRES = ("Action", "Adventure", "Animation", "Children's", "Comedy", "Crime",
          "Documentary", "Drama", "Fantasy", "Film-Noir", "Horror", "Musical",
          "Mystery", "Romance", "Sci-Fi", "Thriller", "War", "Western")
FEATURE_VERSION = "ml1m-event-features-v1"
PROFILE_VERSION = "ml1m-user-profiles-v1"


def digest(path):
    hash_value = hashlib.sha256()
    with open(path, "rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            hash_value.update(block)
    return hash_value.hexdigest()


def parse_boundary(value):
    if not isinstance(value, str) or not value.endswith("Z"):
        raise ValueError("T1/T2 必须是 UTC ISO-8601 时间")
    return int(datetime.fromisoformat(value[:-1] + "+00:00").timestamp())


@dataclass(frozen=True)
class Rating:
    user_id: int
    movie_id: int
    rating: int
    timestamp: int
    sequence: int


class IterationOneDataset:
    def __init__(self, run_dir):
        self.run_dir = Path(run_dir).resolve()
        self.report_path = self.run_dir / "report.json"
        self.cleaned_dir = self.run_dir / "cleaned"
        self.report = self._validate_report()
        self.t1 = parse_boundary(self.report["T1"])
        self.t2 = parse_boundary(self.report["T2"])
        if self.t1 >= self.t2:
            raise ValueError("迭代一时间边界无效：必须满足 T1<T2")
        self.users = self._load_users()
        self.movies = self._load_movies()
        self.ratings = self._load_ratings()
        self.splits = {
            "train": [row for row in self.ratings if row.timestamp <= self.t1],
            "validation": [row for row in self.ratings if self.t1 < row.timestamp <= self.t2],
            "test": [row for row in self.ratings if row.timestamp > self.t2],
        }
        for name, rows in self.splits.items():
            if not rows:
                raise ValueError(f"{name} 时间段没有评分记录，无法完成迭代二实验")

    def _validate_report(self):
        if not self.report_path.is_file():
            raise FileNotFoundError(f"缺少迭代一评估报告：{self.report_path}")
        report = json.loads(self.report_path.read_text(encoding="utf-8"))
        if report.get("status") != "completed":
            raise ValueError("迭代一任务未完成，不能执行迭代二")
        required = ("data_version", "rule_version", "score_version", "T1", "T2", "cleaned_sha256")
        missing = [name for name in required if name not in report]
        if missing:
            raise ValueError("迭代一报告缺少字段：" + "、".join(missing))
        if not self.cleaned_dir.is_dir():
            raise FileNotFoundError(f"缺少迭代一清洗数据目录：{self.cleaned_dir}")
        actual_hashes = {}
        for table in TABLES:
            path = self.cleaned_dir / f"{table}.dat"
            if not path.is_file():
                raise FileNotFoundError(f"缺少迭代一清洗数据：{path}")
            actual_hashes[table] = digest(path)
            if report["cleaned_sha256"].get(table) != actual_hashes[table]:
                raise ValueError(f"{table}.dat 与迭代一登记哈希不一致")
        actual_version = hashlib.sha256(
            json.dumps(actual_hashes, sort_keys=True).encode()).hexdigest()[:16]
        if actual_version != report["data_version"]:
            raise ValueError("清洗数据版本与迭代一报告不一致")
        return report

    def _load_users(self):
        users = {}
        with open(self.cleaned_dir / "users.dat", encoding="iso-8859-1") as source:
            for line_number, line in enumerate(source, 1):
                fields = line.rstrip("\r\n").split("::")
                if len(fields) != 5:
                    raise ValueError(f"users.dat 第 {line_number} 行字段数错误")
                user_id = int(fields[0])
                if user_id in users:
                    raise ValueError(f"users.dat 用户 {user_id} 重复")
                users[user_id] = {"gender": fields[1], "age": int(fields[2]),
                                  "occupation": int(fields[3]), "zip": fields[4]}
        if not users:
            raise ValueError("users.dat 为空")
        return users

    def _load_movies(self):
        movies = {}
        with open(self.cleaned_dir / "movies.dat", encoding="iso-8859-1") as source:
            for line_number, line in enumerate(source, 1):
                fields = line.rstrip("\r\n").split("::")
                if len(fields) != 3:
                    raise ValueError(f"movies.dat 第 {line_number} 行字段数错误")
                movie_id = int(fields[0])
                genres = fields[2].split("|")
                unknown = sorted(set(genres) - set(GENRES))
                if unknown:
                    raise ValueError(f"movies.dat 第 {line_number} 行包含未知类型：{unknown}")
                if movie_id in movies:
                    raise ValueError(f"movies.dat 电影 {movie_id} 重复")
                movies[movie_id] = {"title": fields[1], "genres": genres}
        if not movies:
            raise ValueError("movies.dat 为空")
        return movies

    def _load_ratings(self):
        ratings = []
        with open(self.cleaned_dir / "ratings.dat", encoding="iso-8859-1") as source:
            for sequence, line in enumerate(source):
                fields = line.rstrip("\r\n").split("::")
                if len(fields) != 4:
                    raise ValueError(f"ratings.dat 第 {sequence + 1} 行字段数错误")
                user_id, movie_id, rating, timestamp = map(int, fields)
                if user_id not in self.users or movie_id not in self.movies:
                    raise ValueError(f"ratings.dat 第 {sequence + 1} 行存在孤立引用")
                if rating not in range(1, 6):
                    raise ValueError(f"ratings.dat 第 {sequence + 1} 行评分越界")
                ratings.append(Rating(user_id, movie_id, rating, timestamp, sequence))
        if not ratings:
            raise ValueError("ratings.dat 为空")
        return sorted(ratings, key=lambda row: (row.timestamp, row.sequence))

    def split_summary(self):
        return {name: {"records": len(rows),
                       "positive": sum(row.rating >= 4 for row in rows),
                       "negative": sum(row.rating < 4 for row in rows)}
                for name, rows in self.splits.items()}


def empty_stat():
    return {"count": 0, "sum": 0.0, "positive": 0}


def stat_values(stat, fallback_mean, fallback_positive):
    if not stat["count"]:
        return 0.0, fallback_mean, fallback_positive
    return (math.log1p(stat["count"]), stat["sum"] / stat["count"],
            stat["positive"] / stat["count"])


def event_features(dataset):
    user_stats = defaultdict(empty_stat)
    movie_stats = defaultdict(empty_stat)
    global_stat = empty_stat()
    rows = {"train": [], "validation": [], "test": []}
    labels = {"train": [], "validation": [], "test": []}
    feature_names = ["gender_male", "age_scaled", "occupation_scaled",
                     "user_log_count", "user_mean", "user_positive_rate",
                     "movie_log_count", "movie_mean", "movie_positive_rate",
                     *[f"genre_{name}" for name in GENRES]]

    for rating in dataset.ratings:
        fallback_mean = global_stat["sum"] / global_stat["count"] if global_stat["count"] else 3.5
        fallback_positive = global_stat["positive"] / global_stat["count"] if global_stat["count"] else 0.5
        user_count, user_mean, user_positive = stat_values(
            user_stats[rating.user_id], fallback_mean, fallback_positive)
        movie_count, movie_mean, movie_positive = stat_values(
            movie_stats[rating.movie_id], fallback_mean, fallback_positive)
        user = dataset.users[rating.user_id]
        genres = set(dataset.movies[rating.movie_id]["genres"])
        values = [float(user["gender"] == "M"), user["age"] / 56.0,
                  user["occupation"] / 20.0, user_count, user_mean / 5.0,
                  user_positive, movie_count, movie_mean / 5.0, movie_positive,
                  *[float(name in genres) for name in GENRES]]
        split = "train" if rating.timestamp <= dataset.t1 else (
            "validation" if rating.timestamp <= dataset.t2 else "test")
        rows[split].append(values)
        labels[split].append(int(rating.rating >= 4))
        for stat in (user_stats[rating.user_id], movie_stats[rating.movie_id], global_stat):
            stat["count"] += 1
            stat["sum"] += rating.rating
            stat["positive"] += int(rating.rating >= 4)

    state = {
        "feature_version": FEATURE_VERSION,
        "feature_names": feature_names,
        "cutoff": dataset.report["T1"],
        "users": {},
        "movies": {},
        "user_metadata": {str(key): value for key, value in dataset.users.items()},
        "movie_metadata": {str(key): value for key, value in dataset.movies.items()},
        "global": empty_stat(),
    }
    train_ratings = dataset.splits["train"]
    train_user_stats = defaultdict(empty_stat)
    train_movie_stats = defaultdict(empty_stat)
    for rating in train_ratings:
        for stat in (train_user_stats[rating.user_id], train_movie_stats[rating.movie_id], state["global"]):
            stat["count"] += 1
            stat["sum"] += rating.rating
            stat["positive"] += int(rating.rating >= 4)
    state["users"] = {str(key): value for key, value in train_user_stats.items()}
    state["movies"] = {str(key): value for key, value in train_movie_stats.items()}
    matrices = {name: np.asarray(values, dtype=float) for name, values in rows.items()}
    targets = {name: np.asarray(values, dtype=int) for name, values in labels.items()}
    return matrices, targets, state


def inference_feature(state, user_id, movie_id):
    if str(user_id) not in state["user_metadata"]:
        raise ValueError(f"未知 UserID：{user_id}")
    if str(movie_id) not in state["movie_metadata"]:
        raise ValueError(f"未知 MovieID：{movie_id}")
    global_stat = state["global"]
    fallback_mean = global_stat["sum"] / global_stat["count"] if global_stat["count"] else 3.5
    fallback_positive = global_stat["positive"] / global_stat["count"] if global_stat["count"] else 0.5
    user_stat = state["users"].get(str(user_id), empty_stat())
    movie_stat = state["movies"].get(str(movie_id), empty_stat())
    user_count, user_mean, user_positive = stat_values(user_stat, fallback_mean, fallback_positive)
    movie_count, movie_mean, movie_positive = stat_values(movie_stat, fallback_mean, fallback_positive)
    user = state["user_metadata"][str(user_id)]
    genres = set(state["movie_metadata"][str(movie_id)]["genres"])
    values = [float(user["gender"] == "M"), user["age"] / 56.0,
              user["occupation"] / 20.0, user_count, user_mean / 5.0,
              user_positive, movie_count, movie_mean / 5.0, movie_positive,
              *[float(name in genres) for name in GENRES]]
    return np.asarray([values], dtype=float)


def user_profiles(dataset, min_history=2):
    genre_index = {name: index for index, name in enumerate(GENRES)}
    sums = defaultdict(lambda: np.zeros(len(GENRES), dtype=float))
    counts = defaultdict(lambda: np.zeros(len(GENRES), dtype=float))
    totals = defaultdict(empty_stat)
    for rating in dataset.splits["train"]:
        totals[rating.user_id]["count"] += 1
        totals[rating.user_id]["sum"] += rating.rating
        totals[rating.user_id]["positive"] += int(rating.rating >= 4)
        for genre in dataset.movies[rating.movie_id]["genres"]:
            position = genre_index[genre]
            sums[rating.user_id][position] += rating.rating
            counts[rating.user_id][position] += 1
    eligible = sorted(user_id for user_id, stat in totals.items() if stat["count"] >= min_history)
    insufficient = sorted(set(dataset.users) - set(eligible))
    if len(eligible) < 3:
        raise ValueError("训练期历史充足的用户少于 3 个，无法聚类和降维")
    global_genre_sum = sum((sums[user_id] for user_id in eligible), np.zeros(len(GENRES)))
    global_genre_count = sum((counts[user_id] for user_id in eligible), np.zeros(len(GENRES)))
    global_means = np.divide(global_genre_sum, global_genre_count,
                             out=np.full(len(GENRES), 3.0), where=global_genre_count > 0)
    matrix = []
    details = {}
    for user_id in eligible:
        means = np.divide(sums[user_id], counts[user_id], out=global_means.copy(),
                          where=counts[user_id] > 0)
        frequencies = counts[user_id] / max(1, totals[user_id]["count"])
        overall_mean = totals[user_id]["sum"] / totals[user_id]["count"]
        positive_rate = totals[user_id]["positive"] / totals[user_id]["count"]
        vector = np.concatenate((means / 5.0, frequencies,
                                 [math.log1p(totals[user_id]["count"]), overall_mean / 5.0,
                                  positive_rate]))
        matrix.append(vector)
        ranked = sorted(zip(GENRES, means, counts[user_id]), key=lambda item: (-item[1], -item[2], item[0]))
        details[str(user_id)] = {
            "history_count": totals[user_id]["count"],
            "mean_rating": round(overall_mean, 4),
            "positive_rate": round(positive_rate, 4),
            "top_genres": [{"genre": genre, "mean_rating": round(float(mean), 4),
                            "ratings": int(count)} for genre, mean, count in ranked[:3]],
        }
    feature_names = ([f"mean_{name}" for name in GENRES]
                     + [f"frequency_{name}" for name in GENRES]
                     + ["log_history_count", "overall_mean", "positive_rate"])
    return {"user_ids": eligible, "insufficient_user_ids": insufficient,
            "matrix": np.asarray(matrix, dtype=float), "details": details,
            "feature_names": feature_names, "profile_version": PROFILE_VERSION,
            "min_history": min_history}
