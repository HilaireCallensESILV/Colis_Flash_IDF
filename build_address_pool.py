"""À lancer UNE fois : construit data/address_pool.csv à partir de la Base Adresse Nationale.
Télécharge plusieurs centaines de Mo (8 départements d'Île-de-France). Usage : python build_address_pool.py"""
from pathlib import Path

from engine import AddressPool
from models import Params

if __name__ == "__main__":
    AddressPool.build_from_ban(Params(), Path(__file__).resolve().parent / "data")
