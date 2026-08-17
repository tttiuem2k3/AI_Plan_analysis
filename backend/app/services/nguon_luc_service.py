from backend.app.repositories.nguon_luc_repo import CongDoanLonRepo, CongDoanRepo, NguonLucRepo, CongDoanNguonLucRepo, CongDoanCongDoanLonRepo
from backend.app.schemas.nguon_luc_schema import CongDoanLonSchema, CongDoanSchema, NguonLucSchema, CongDoanNguonLucSchema, CongDoanCongDoanLonSchema
from sqlalchemy.orm import Session

class CongDoanLonService:
    @staticmethod
    def get_all(db: Session):
        return CongDoanLonRepo.get_all(db)
    @staticmethod
    def create(db: Session, data: CongDoanLonSchema):
        return CongDoanLonRepo.create(db, data)
    @staticmethod
    def update(db: Session, ma: str, data: CongDoanLonSchema):
        return CongDoanLonRepo.update(db, ma, data)
    @staticmethod
    def delete(db: Session, ma: str):
        return CongDoanLonRepo.delete(db, ma)

class CongDoanService:
    @staticmethod
    def get_all(db: Session):
        return CongDoanRepo.get_all(db)
    @staticmethod
    def create(db: Session, data: CongDoanSchema):
        return CongDoanRepo.create(db, data)
    @staticmethod
    def update(db: Session, ma: str, data: CongDoanSchema):
        return CongDoanRepo.update(db, ma, data)
    @staticmethod
    def delete(db: Session, ma: str):
        return CongDoanRepo.delete(db, ma)

class NguonLucService:
    @staticmethod
    def get_all(db: Session):
        return NguonLucRepo.get_all(db)

    @staticmethod
    def get_one(db: Session, ma: str):
        return NguonLucRepo.get_one(db, ma)

    @staticmethod
    def create(db: Session, data: NguonLucSchema):
        return NguonLucRepo.create(db, data)
    @staticmethod
    def update(db: Session, ma: str, data: NguonLucSchema):
        return NguonLucRepo.update(db, ma, data)
    @staticmethod
    def delete(db: Session, ma: str):
        return NguonLucRepo.delete(db, ma)

class CongDoanNguonLucService:
    @staticmethod
    def get_all(db: Session):
        return CongDoanNguonLucRepo.get_all(db)
    @staticmethod
    def create(db: Session, data: CongDoanNguonLucSchema):
        return CongDoanNguonLucRepo.create(db, data)
    @staticmethod
    def delete(db: Session, ma_cong_doan: str, ma_nguon_luc: str):
        return CongDoanNguonLucRepo.delete(db, ma_cong_doan, ma_nguon_luc)

class CongDoanCongDoanLonService:
    @staticmethod
    def get_all(db: Session):
        return CongDoanCongDoanLonRepo.get_all(db)

    @staticmethod
    def attach(db: Session, data: CongDoanCongDoanLonSchema):
        return CongDoanCongDoanLonRepo.attach(db, data)

    @staticmethod
    def detach(db: Session, ma_cong_doan: str):
        return CongDoanCongDoanLonRepo.detach(db, ma_cong_doan)

    @staticmethod
    def get_congdoanlon_of_congdoan(db: Session, ma_cong_doan: str):
        return CongDoanCongDoanLonRepo.get_congdoanlon_of_congdoan(db, ma_cong_doan)
